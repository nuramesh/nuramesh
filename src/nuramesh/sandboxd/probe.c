/* The sandbox service's probe (real-models spec R52, #963). A static binary run INSIDE the sandbox.
   Prints "R <id> PASS|FAIL <detail>" (judged here) and "I <key> <value>" (judged by the caller).
   Mode "selftest <root listing>" is the self-test of R52: the supervisor (nuramesh.sandboxd) runs
   it on the built-in root filesystem and is `ready` only when every line it needs says PASS.
   The other modes are the escape matrix of the C2 spike (7.9), used by the tests. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/personality.h>
#include <sys/prctl.h>
#include <sys/ptrace.h>
#include <sys/resource.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/sysmacros.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static void R(const char *id, int ok, const char *fmt, ...) __attribute__((format(printf, 3, 4)));
#include <stdarg.h>
static void R(const char *id, int ok, const char *fmt, ...) {
  va_list ap; va_start(ap, fmt);
  printf("R %s %s ", id, ok ? "PASS" : "FAIL"); vprintf(fmt, ap); printf("\n"); va_end(ap); fflush(stdout);
}
/* expect a syscall to fail with a given errno */
static void deny(const char *id, long rc, int want) {
  int e = errno;
  R(id, rc == -1 && e == want, "rc=%ld errno=%d(%s) want=%d", rc, rc == -1 ? e : 0, rc == -1 ? strerror(e) : "-", want);
}
static void *thr(void *a) { return a; }

static void status_checks(void) {
  FILE *f = fopen("/proc/self/status", "r"); char l[256]; int nnp = -1, sec = -1, filt = -1;
  unsigned long long eff = 1, prm = 1, inh = 1, bnd = 1, amb = 1;
  if (!f) { R("status.read", 0, "cannot open /proc/self/status"); return; }
  while (fgets(l, sizeof l, f)) {
    sscanf(l, "CapEff: %llx", &eff); sscanf(l, "CapPrm: %llx", &prm); sscanf(l, "CapInh: %llx", &inh);
    sscanf(l, "CapBnd: %llx", &bnd); sscanf(l, "CapAmb: %llx", &amb);
    sscanf(l, "NoNewPrivs: %d", &nnp); sscanf(l, "Seccomp: %d", &sec); sscanf(l, "Seccomp_filters: %d", &filt);
  }
  fclose(f);
  R("caps.eff_zero", eff == 0, "CapEff=%llx", eff);
  R("caps.prm_inh_amb_zero", prm == 0 && inh == 0 && amb == 0, "CapPrm=%llx CapInh=%llx CapAmb=%llx", prm, inh, amb);
  R("caps.bnd_zero", bnd == 0, "CapBnd=%llx", bnd);
  R("nnp.on", nnp == 1, "NoNewPrivs=%d", nnp);
  R("seccomp.mode_filter", sec == 2, "Seccomp=%d", sec);
  printf("I seccomp_filters %d\n", filt);
  R("id.uid_gid_65532", getuid() == 65532 && getgid() == 65532 && geteuid() == 65532, "uid=%d euid=%d gid=%d", getuid(), geteuid(), getgid());
}

static void seccomp_checks(void) {
  errno = 0; deny("sc.unshare0_EPERM", syscall(SYS_unshare, 0), EPERM);
  errno = 0; deny("sc.unshare_newuser", syscall(SYS_unshare, CLONE_NEWUSER), EPERM);
  errno = 0; deny("sc.unshare_newnet", syscall(SYS_unshare, CLONE_NEWNET), EPERM);
  errno = 0; deny("sc.setns", syscall(SYS_setns, -1, 0), EPERM);
  { errno = 0; long p = syscall(SYS_clone, CLONE_NEWUSER | SIGCHLD, 0, 0, 0, 0); int e = errno;
    if (p == 0) _exit(0);
    if (p > 0) waitpid(p, 0, 0);
    errno = e; deny("sc.clone_newuser", p, EPERM); }
  { errno = 0; long p = syscall(SYS_clone, CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWNET | SIGCHLD, 0, 0, 0, 0); int e = errno;
    if (p == 0) _exit(0);
    if (p > 0) waitpid(p, 0, 0);
    errno = e; deny("sc.clone_newns_pid_net", p, EPERM); }
  { static const struct { const char *id; unsigned long flag; } un[] = {
      {"sc.unshare_newns", CLONE_NEWNS}, {"sc.unshare_newuts", CLONE_NEWUTS}, {"sc.unshare_newipc", CLONE_NEWIPC},
      {"sc.unshare_newpid", CLONE_NEWPID}, {"sc.unshare_newcgroup", CLONE_NEWCGROUP}, {"sc.unshare_newtime", 0x00000080}, {0, 0}};
    for (int i = 0; un[i].id; i++) { errno = 0; deny(un[i].id, syscall(SYS_unshare, un[i].flag), EPERM); } }
  { static const struct { const char *id; unsigned long flag; } ns[] = {
      {"sc.clone_newns", CLONE_NEWNS}, {"sc.clone_newpid", CLONE_NEWPID}, {"sc.clone_newnet", CLONE_NEWNET},
      {"sc.clone_newipc", CLONE_NEWIPC}, {"sc.clone_newuts", CLONE_NEWUTS}, {"sc.clone_newcgroup", CLONE_NEWCGROUP},
      {"sc.clone_newtime", 0x00000080}, {0, 0}};
    for (int i = 0; ns[i].id; i++) { errno = 0; long p = syscall(SYS_clone, ns[i].flag | SIGCHLD, 0, 0, 0, 0); int e = errno;
      if (p == 0) _exit(0);
      if (p > 0) waitpid(p, 0, 0);
      errno = e; deny(ns[i].id, p, EPERM); } }
  errno = 0; deny("sc.clone3_ENOSYS", syscall(435, 0, 0), ENOSYS);
  { pid_t p = fork(); if (p == 0) _exit(7); int st = 0; if (p > 0) waitpid(p, &st, 0);
    R("sc.fork_works", p > 0 && WIFEXITED(st) && WEXITSTATUS(st) == 7, "pid=%d", p > 0); }
  { pthread_t t; int rc = pthread_create(&t, 0, thr, 0); if (!rc) pthread_join(t, 0);
    R("sc.thread_works", rc == 0, "pthread_create rc=%d", rc); }
  errno = 0; deny("sc.mount", mount("none", "/tmp", "tmpfs", 0, 0), EPERM);
  errno = 0; deny("sc.umount2", umount2("/tmp", MNT_DETACH), EPERM);
  errno = 0; deny("sc.pivot_root", syscall(SYS_pivot_root, ".", "."), EPERM);
  errno = 0; deny("sc.fsopen", syscall(430, "tmpfs", 0), EPERM);
  errno = 0; deny("sc.fsconfig", syscall(431, -1, 0, 0, 0, 0), EPERM);
  errno = 0; deny("sc.fsmount", syscall(432, -1, 0, 0), EPERM);
  errno = 0; deny("sc.fspick", syscall(433, AT_FDCWD, "/", 0), EPERM);
  errno = 0; deny("sc.move_mount", syscall(429, -1, "", -1, "", 0), EPERM);
  errno = 0; deny("sc.open_tree", syscall(428, AT_FDCWD, "/", 0), EPERM);
  errno = 0; deny("sc.mount_setattr", syscall(442, -1, "", 0, 0, 0), EPERM);
  errno = 0; deny("sc.keyctl", syscall(SYS_keyctl, 0 /*GET_KEYRING_ID*/, -3 /*session*/, 0), EPERM);
  errno = 0; deny("sc.add_key", syscall(SYS_add_key, "user", "spike", "x", 1, -2 /*process*/), EPERM);
  errno = 0; deny("sc.request_key", syscall(SYS_request_key, "user", "spike-none", 0, 0), EPERM);
  { char attr[128]; memset(attr, 0, sizeof attr);
    errno = 0; deny("sc.bpf", syscall(SYS_bpf, 0 /*MAP_CREATE*/, attr, sizeof attr), EPERM); }
  { char attr[128]; memset(attr, 0, sizeof attr); attr[4] = 112; /* size */
    errno = 0; deny("sc.perf_event_open", syscall(SYS_perf_event_open, attr, 0, -1, -1, 0), EPERM); }
  errno = 0; deny("sc.userfaultfd", syscall(SYS_userfaultfd, 1 /*UFFD_USER_MODE_ONLY*/), EPERM);
  { char p[256]; memset(p, 0, sizeof p);
    errno = 0; deny("sc.io_uring_setup", syscall(425, 4, p), EPERM); }
  errno = 0; deny("sc.io_uring_enter", syscall(426, -1, 0, 0, 0, 0, 0), EPERM);
  errno = 0; deny("sc.io_uring_register", syscall(427, -1, 0, 0, 0), EPERM);
  { pid_t p = fork(); if (p == 0) { errno = 0; long r = ptrace(PTRACE_TRACEME, 0, 0, 0); _exit(r == -1 && errno == EPERM ? 0 : 1); }
    int st = 1; waitpid(p, &st, 0); R("sc.ptrace_traceme", WIFEXITED(st) && WEXITSTATUS(st) == 0, "child exit=%d", WEXITSTATUS(st)); }
  errno = 0; deny("sc.ptrace_attach", ptrace(PTRACE_ATTACH, getppid(), 0, 0), EPERM);
  errno = 0; deny("sc.process_vm_readv", syscall(SYS_process_vm_readv, getppid(), 0, 0, 0, 0, 0), EPERM);
  errno = 0; deny("sc.process_vm_writev", syscall(SYS_process_vm_writev, getppid(), 0, 0, 0, 0, 0), EPERM);
  errno = 0; deny("sc.open_tree_attr", syscall(467, AT_FDCWD, "/", 0, 0, 0), EPERM);
  /* foreign ABIs */
  { pid_t p = fork();
    if (p == 0) { long r; __asm__ volatile("int $0x80" : "=a"(r) : "a"(20L) : "memory", "r8", "r9", "r10", "r11"); _exit(r == -EPERM ? 0 : (r > 0 ? 1 : 2)); }
    int st = 0; waitpid(p, &st, 0);
    R("sc.i386_abi_refused", WIFEXITED(st) && WEXITSTATUS(st) == 0, "int80 getpid: %s %d", WIFEXITED(st) ? "exit" : "signal", WIFEXITED(st) ? WEXITSTATUS(st) : WTERMSIG(st)); }
  errno = 0; deny("sc.x32_number_refused", syscall(0x40000000 | 39), EPERM);
  /* gated by capabilities (not by the filter): must fail whatever the errno */
  errno = 0; { long r = sethostname("x", 1); R("cap.sethostname", r == -1, "errno=%d", errno); }
  errno = 0; { long r = chroot("/"); R("cap.chroot", r == -1, "errno=%d", errno); }
  errno = 0; { long r = mknod("/tmp/spike-null", S_IFCHR | 0666, makedev(1, 3)); R("cap.mknod_chardev", r == -1, "errno=%d", errno); }
  errno = 0; { long r = syscall(SYS_reboot, 0xfee1dead, 672274793, 0x01234567, 0); R("cap.reboot", r == -1, "errno=%d", errno); }
  errno = 0; { long r = syscall(SYS_init_module, 0, 0, ""); R("cap.init_module", r == -1 && errno == EPERM, "errno=%d", errno); }
  errno = 0; { long r = syscall(SYS_syslog, 3, 0, 0); printf("I syslog_read rc=%ld errno=%d\n", r, errno); }
  errno = 0; { long r = setuid(0); int e1 = errno; long r2 = setgid(0); int e2 = errno; gid_t g = 0; long r3 = setgroups(1, &g);
    R("id.setuid0_setgid0_setgroups_fail", r == -1 && r2 == -1 && r3 == -1, "setuid errno=%d setgid errno=%d setgroups errno=%d", e1, e2, errno); }
}

static void try_connect(const char *id, const char *ip, int port) {
  int s = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0); struct sockaddr_in a; memset(&a, 0, sizeof a);
  a.sin_family = AF_INET; a.sin_port = htons(port); inet_pton(AF_INET, ip, &a.sin_addr);
  errno = 0; int rc = connect(s, (struct sockaddr *)&a, sizeof a); int e = errno;
  if (rc == -1 && e == EINPROGRESS) { /* wait up to 2 s for the verdict */
    fd_set w; FD_ZERO(&w); FD_SET(s, &w); struct timeval tv = {2, 0}; socklen_t l = sizeof e;
    if (select(s + 1, 0, &w, 0, &tv) == 1) { getsockopt(s, SOL_SOCKET, SO_ERROR, &e, &l); if (e == 0) rc = 0; } else e = ETIMEDOUT; }
  close(s);
  R(id, rc == -1 && (e == ENETUNREACH || e == ECONNREFUSED || e == EHOSTUNREACH || e == ETIMEDOUT), "%s:%d rc=%d errno=%d(%s)", ip, port, rc, e, strerror(e));
}
static void net_checks(const char *abstract) {
  FILE *f = fopen("/proc/net/dev", "r"); char l[512]; int n = 0, other = 0; char names[256] = "";
  if (f) { while (fgets(l, sizeof l, f)) { char *c = strchr(l, ':'); if (!c) continue; *c = 0; char *p = l; while (*p == ' ') p++;
      n++; if (strcmp(p, "lo")) other++; strncat(names, p, 40); strcat(names, ","); } fclose(f); }
  R("net.only_lo", f && n == 1 && other == 0, "interfaces=%s", names);
  f = fopen("/proc/net/route", "r"); n = 0; if (f) { while (fgets(l, sizeof l, f)) n++; fclose(f); }
  R("net.no_route", f && n <= 1, "route lines (with header)=%d", n);
  try_connect("net.metadata_169_254", "169.254.169.254", 80);
  try_connect("net.private_10", "10.0.0.4", 5432);
  try_connect("net.internet", "1.1.1.1", 443);
  try_connect("net.loopback_22", "127.0.0.1", 22);
  { int s = socket(AF_INET, SOCK_DGRAM, 0); struct sockaddr_in a; memset(&a, 0, sizeof a); a.sin_family = AF_INET; a.sin_port = htons(53);
    inet_pton(AF_INET, "8.8.8.8", &a.sin_addr); errno = 0; long rc = sendto(s, "x", 1, 0, (struct sockaddr *)&a, sizeof a);
    R("net.udp_dns", rc == -1, "sendto rc=%ld errno=%d(%s)", rc, errno, strerror(errno)); close(s); }
  { errno = 0; int s = socket(AF_PACKET, SOCK_RAW, 0); R("net.af_packet", s == -1, "errno=%d", errno); if (s >= 0) close(s); }
  if (abstract && *abstract) {
    int s = socket(AF_UNIX, SOCK_STREAM, 0); struct sockaddr_un a; memset(&a, 0, sizeof a); a.sun_family = AF_UNIX;
    strncpy(a.sun_path + 1, abstract, sizeof a.sun_path - 2);
    errno = 0; int rc = connect(s, (struct sockaddr *)&a, sizeof(sa_family_t) + 1 + strlen(abstract));
    R("net.abstract_unix_socket_of_supervisor", rc == -1 && errno == ECONNREFUSED, "rc=%d errno=%d(%s)", rc, errno, strerror(errno)); close(s);
  }
}

static int wr(const char *p, int flags) { int fd = open(p, flags, 0600); int e = errno; if (fd >= 0) close(fd); errno = e; return fd; }
static void fs_checks(const char *want_root) {
  DIR *d; struct dirent *de; char buf[2048] = ""; int n = 0;
  /* fds */
  d = opendir("/proc/self/fd"); int dfd = d ? dirfd(d) : -1; int bad = 0;
  while (d && (de = readdir(d))) { if (de->d_name[0] == '.') continue; int fd = atoi(de->d_name); if (fd == dfd) continue;
    char t[256] = "", lp[64]; snprintf(lp, sizeof lp, "/proc/self/fd/%d", fd); ssize_t k = readlink(lp, t, sizeof t - 1); if (k > 0) t[k] = 0;
    snprintf(buf + strlen(buf), sizeof buf - strlen(buf), "%d=%s ", fd, t); if (fd > 2) bad++; n++; }
  if (d) closedir(d);
  R("fd.only_0_1_2", d && bad == 0 && n == 3, "%s", buf);
  { char t[256] = ""; ssize_t k = readlink("/proc/self/fd/0", t, sizeof t - 1); if (k > 0) t[k] = 0; R("fd.stdin_dev_null", !strcmp(t, "/dev/null"), "fd0=%s", t); }
  /* processes */
  buf[0] = 0; n = 0; d = opendir("/proc");
  while (d && (de = readdir(d))) { if (de->d_name[0] < '0' || de->d_name[0] > '9') continue; n++;
    char p[300], c[64] = "?"; snprintf(p, sizeof p, "/proc/%s/comm", de->d_name); FILE *f = fopen(p, "r"); if (f) { if (fgets(c, sizeof c, f)) c[strcspn(c, "\n")] = 0; fclose(f); }
    snprintf(buf + strlen(buf), sizeof buf - strlen(buf), "%s(%s) ", de->d_name, c); }
  if (d) closedir(d);
  R("pid.only_own_tree", n >= 1 && n <= 2 && getpid() <= 3, "self=%d procs=%s", getpid(), buf);
  /* root listing */
  buf[0] = 0; struct dirent **nl; n = scandir("/", &nl, 0, alphasort);
  for (int i = 0; i < n; i++) { if (nl[i]->d_name[0] != '.') { strcat(buf, nl[i]->d_name); strcat(buf, " "); } }
  if (strlen(buf)) buf[strlen(buf) - 1] = 0;
  R("fs.root_listing_is_rootfs", !strcmp(buf, want_root), "got='%s' want='%s'", buf, want_root);
  const char *absent[] = {"/sys", "/home", "/etc", "/run", "/var", "/root", "/images", "/work", "/spike", "/usr", "/mnt", "/boot", "/dev/mem", "/dev/kmsg", "/dev/sda", "/proc/1/root/home", 0};
  for (int i = 0; absent[i]; i++) { struct stat st; char id[80]; snprintf(id, sizeof id, "fs.absent:%s", absent[i]); errno = 0;
    int rc = stat(absent[i], &st); R(id, rc == -1, "errno=%d(%s)", errno, strerror(errno)); }
  errno = 0; R("fs.root_readonly", wr("/spike-x", O_CREAT | O_WRONLY) == -1 && errno == EROFS, "errno=%d(%s)", errno, strerror(errno));
  errno = 0; R("fs.bin_readonly", wr("/bin/spike-x", O_CREAT | O_WRONLY) == -1 && errno == EROFS, "errno=%d(%s)", errno, strerror(errno));
  errno = 0; R("fs.busybox_not_writable", wr("/bin/busybox", O_WRONLY) == -1, "errno=%d(%s)", errno, strerror(errno));
  errno = 0; R("fs.mkdir_root_fails", mkdir("/spike-d", 0700) == -1 && errno == EROFS, "errno=%d(%s)", errno, strerror(errno));
  errno = 0; R("proc.oom_score_adj_not_writable", wr("/proc/self/oom_score_adj", O_WRONLY) == -1 && errno == EROFS, "errno=%d(%s)", errno, strerror(errno));
  errno = 0; R("proc.sys_not_writable", wr("/proc/sys/kernel/hostname", O_WRONLY) == -1, "errno=%d(%s)", errno, strerror(errno));
  errno = 0; R("proc.sysrq_not_writable", wr("/proc/sysrq-trigger", O_WRONLY) == -1, "errno=%d(%s)", errno, strerror(errno));
  errno = 0; R("proc.userns_limit_not_writable", wr("/proc/sys/user/max_user_namespaces", O_WRONLY) == -1, "errno=%d(%s)", errno, strerror(errno));
  errno = 0; R("fs.workspace_writable", wr("/workspace/spike-w", O_CREAT | O_WRONLY) >= 0, "errno=%d", errno);
  errno = 0; R("fs.tmp_writable", wr("/tmp/spike-w", O_CREAT | O_WRONLY) >= 0, "errno=%d", errno);
  { char cwd[256] = ""; if (!getcwd(cwd, sizeof cwd)) cwd[0] = 0; R("fs.cwd_workspace", !strcmp(cwd, "/workspace"), "cwd=%s", cwd); }
  /* /dev */
  buf[0] = 0; n = scandir("/dev", &nl, 0, alphasort);
  for (int i = 0; i < n; i++) if (nl[i]->d_name[0] != '.') { strcat(buf, nl[i]->d_name); strcat(buf, " "); }
  printf("I dev_listing %s\n", buf);
  /* mounts: every mount nosuid; nodev except /dev */
  FILE *f = fopen("/proc/self/mountinfo", "r"); char l[1024]; int nosuid_bad = 0, total = 0;
  while (f && fgets(l, sizeof l, f)) { char mp[256], opts[256]; if (sscanf(l, "%*d %*d %*s %*s %255s %255s", mp, opts) != 2) continue; total++;
    int ns = strstr(opts, "nosuid") != 0; if (!ns) nosuid_bad++; l[strcspn(l, "\n")] = 0; printf("I mount %s\n", l); }
  if (f) fclose(f);
  R("mnt.all_nosuid", f && total > 0 && nosuid_bad == 0, "mounts=%d without_nosuid=%d", total, nosuid_bad);
  /* masked /proc paths of the engine: informational */
  const char *masked[] = {"/proc/kcore", "/proc/keys", "/proc/timer_list", "/proc/sched_debug", "/proc/scsi/scsi", "/proc/acpi", "/proc/latency_stats", "/proc/asound", "/proc/interrupts", "/proc/timer_stats", "/proc/kallsyms", "/proc/meminfo", "/proc/1/environ", "/proc/sys/kernel/core_pattern", 0};
  for (int i = 0; masked[i]; i++) { errno = 0; int fd = open(masked[i], O_RDONLY | O_NONBLOCK); char b[64]; ssize_t k = -1; if (fd >= 0) { k = read(fd, b, sizeof b); close(fd); }
    printf("I procread %s %s errno=%d bytes=%zd\n", masked[i], fd >= 0 ? "OPENED" : "refused", fd >= 0 ? 0 : errno, k); }
}

static void misc(void) {
  for (char **e = environ; *e; e++) printf("I env %s\n", *e);
  const char *ns[] = {"user", "pid", "net", "ipc", "uts", "cgroup", "mnt", "time", 0};
  for (int i = 0; ns[i]; i++) { char p[64], t[128] = "?"; snprintf(p, sizeof p, "/proc/self/ns/%s", ns[i]); ssize_t k = readlink(p, t, sizeof t - 1); if (k > 0) t[k] = 0; printf("I ns %s %s\n", ns[i], t); }
  char h[128] = ""; gethostname(h, sizeof h); printf("I hostname %s\n", h);
  FILE *f = fopen("/proc/self/cgroup", "r"); char l[256]; while (f && fgets(l, sizeof l, f)) { l[strcspn(l, "\n")] = 0; printf("I cgroup %s\n", l); } if (f) fclose(f);
  f = fopen("/proc/self/uid_map", "r"); while (f && fgets(l, sizeof l, f)) { l[strcspn(l, "\n")] = 0; printf("I uid_map %s\n", l); } if (f) fclose(f);
  f = fopen("/proc/self/oom_score_adj", "r"); if (f && fgets(l, sizeof l, f)) { l[strcspn(l, "\n")] = 0; printf("I oom_score_adj %s\n", l); } if (f) fclose(f);
  struct rlimit r; getrlimit(RLIMIT_CORE, &r); printf("I rlimit_core %llu\n", (unsigned long long)r.rlim_cur);
  getrlimit(RLIMIT_NOFILE, &r); printf("I rlimit_nofile %llu\n", (unsigned long long)r.rlim_cur);
  printf("I sid %d pid %d pgid %d\n", getsid(0), getpid(), getpgid(0));
  errno = 0; int t = open("/dev/tty", O_RDWR); R("tty.no_controlling_terminal", t == -1, "errno=%d(%s)", errno, strerror(errno)); if (t >= 0) close(t);
}

static long fill(const char *path) { /* write zeros until an error; returns MiB written */
  int fd = open(path, O_CREAT | O_WRONLY | O_TRUNC, 0600); if (fd < 0) return -1;
  static char b[1 << 20]; long mib = 0; while (mib < 4096) { ssize_t k = write(fd, b, sizeof b); if (k != (ssize_t)sizeof b) break; mib++; }
  int e = errno; close(fd); errno = e; return mib;
}

int main(int argc, char **argv) {
  const char *mode = argc > 1 ? argv[1] : "matrix";
  setvbuf(stdout, 0, _IOLBF, 0);
  if (!strcmp(mode, "matrix")) {
    status_checks(); seccomp_checks(); net_checks(argc > 2 ? argv[2] : ""); fs_checks(argc > 3 ? argv[3] : "bin dev proc tmp workspace"); misc();
    printf("I done 1\n"); return 0;
  }
  if (!strcmp(mode, "selftest")) { /* R52: exactly the checks the supervisor needs, on any root filesystem */
    const char *want = argc > 2 ? argv[2] : "";
    status_checks();
    { FILE *f = fopen("/proc/self/status", "r"); char l[256]; int filt = -1;
      while (f && fgets(l, sizeof l, f)) sscanf(l, "Seccomp_filters: %d", &filt);
      if (f) fclose(f);
      R("seccomp.filters_at_least_2", filt >= 2, "Seccomp_filters=%d", filt); }
    /* Only the pinned profile of R47 refuses this one (the command's own filter allows it): an
       allow-all profile in the container has Seccomp 2 and two filters, and still fails here. */
    errno = 0; deny("profile.personality_refused", syscall(SYS_personality, ADDR_NO_RANDOMIZE), EPERM);
    errno = 0; deny("sc.unshare0_EPERM", syscall(SYS_unshare, 0), EPERM);
    errno = 0; deny("sc.unshare_newuser", syscall(SYS_unshare, CLONE_NEWUSER), EPERM);
    errno = 0; deny("sc.mount", mount("none", "/tmp", "tmpfs", 0, 0), EPERM);
    { FILE *f = fopen("/proc/net/dev", "r"); char l[512]; int n = 0, other = 0;
      while (f && fgets(l, sizeof l, f)) { char *c = strchr(l, ':'); if (!c) continue; *c = 0; char *p = l; while (*p == ' ') p++; n++; if (strcmp(p, "lo")) other++; }
      if (f) fclose(f);
      R("net.only_lo", f && n == 1 && other == 0, "interfaces=%d other=%d", n, other); }
    { DIR *d = opendir("/proc/self/fd"); struct dirent *de; int dfd = d ? dirfd(d) : -1, bad = 0, n = 0;
      while (d && (de = readdir(d))) { if (de->d_name[0] == '.') continue; int fd = atoi(de->d_name); if (fd == dfd) continue; if (fd > 2) bad++; n++; }
      if (d) closedir(d);
      R("fd.only_0_1_2", d && bad == 0 && n == 3, "descriptors=%d above_2=%d", n, bad); }
    { DIR *d = opendir("/proc"); struct dirent *de; int n = 0;
      while (d && (de = readdir(d))) if (de->d_name[0] >= '0' && de->d_name[0] <= '9') n++;
      if (d) closedir(d);
      R("pid.only_own_tree", d && n >= 1 && n <= 2 && getpid() <= 3, "self=%d processes=%d", getpid(), n); }
    { char buf[4096] = ""; struct dirent **nl; int n = scandir("/", &nl, 0, alphasort);
      for (int i = 0; i < n; i++) if (nl[i]->d_name[0] != '.' && strlen(buf) + strlen(nl[i]->d_name) + 2 < sizeof buf) { strcat(buf, nl[i]->d_name); strcat(buf, " "); }
      if (strlen(buf)) buf[strlen(buf) - 1] = 0;
      R("fs.root_listing_is_rootfs", n >= 0 && !strcmp(buf, want), "got='%s'", buf); }
    const char *absent[] = {"/sys", "/run/sandbox", "/images", "/work", 0};
    for (int i = 0; absent[i]; i++) { struct stat st; char id[80]; snprintf(id, sizeof id, "fs.absent:%s", absent[i]); errno = 0;
      int rc = lstat(absent[i], &st); R(id, rc == -1 && (errno == ENOENT || errno == ENOTDIR), "errno=%d(%s)", errno, strerror(errno)); }
    errno = 0; R("fs.root_readonly", wr("/nuramesh-selftest-x", O_CREAT | O_WRONLY) == -1 && errno == EROFS, "errno=%d(%s)", errno, strerror(errno));
    errno = 0; R("fs.usr_readonly", wr("/usr/nuramesh-selftest-x", O_CREAT | O_WRONLY) == -1 && errno == EROFS, "errno=%d(%s)", errno, strerror(errno));
    printf("I done 1\n"); return 0;
  }
  if (!strcmp(mode, "sleep-tree")) { /* 3 children, one grandchild in its own session, all sleeping */
    for (int i = 0; i < 3; i++) if (fork() == 0) { if (i == 2 && fork() == 0) { setsid(); signal(SIGTERM, SIG_IGN); signal(SIGHUP, SIG_IGN); } pause(); sleep(100000); _exit(0); }
    printf("I sleeping 1\n"); sleep(100000); return 0;
  }
  if (!strcmp(mode, "marker-write")) {
    int a = wr("/tmp/marker", O_CREAT | O_WRONLY), b = wr("/dev/shm/marker", O_CREAT | O_WRONLY), c = wr("/workspace/marker", O_CREAT | O_WRONLY);
    R("marker.written", a >= 0 && b >= 0 && c >= 0, "tmp=%d shm=%d ws=%d", a >= 0, b >= 0, c >= 0); return 0;
  }
  if (!strcmp(mode, "marker-check")) {
    struct stat st; int a = stat("/tmp/marker", &st), b = stat("/dev/shm/marker", &st), c = stat("/workspace/marker", &st);
    R("marker.absent_in_next_request", a == -1 && b == -1 && c == -1, "tmp=%d shm=%d ws=%d (-1 = absent)", a, b, c); return 0;
  }
  if (!strcmp(mode, "memhog")) { /* anonymous memory until killed, at most 2 GiB */
    long mib = 0; while (mib < 2048) { char *p = mmap(0, 1 << 20, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0); if (p == MAP_FAILED) break; memset(p, 1, 1 << 20); mib++; if (mib % 32 == 0) printf("I memhog_mib %ld\n", mib); }
    printf("I memhog_end %ld\n", mib); return 3;
  }
  if (!strcmp(mode, "tmpfill")) { /* fill one tmpfs path */
    errno = 0; long m = fill(argv[2]); printf("I tmpfill %s mib=%ld errno=%d(%s)\n", argv[2], m, errno, strerror(errno)); sleep(2); return 4;
  }
  if (!strcmp(mode, "forkbomb")) { /* bounded by the pids limit; at most 4096 */
    int n = 0; for (; n < 4096; n++) { pid_t p = fork(); if (p == 0) { pause(); sleep(100000); _exit(0); } if (p < 0) break; }
    printf("I forkbomb_children %d errno=%d(%s)\n", n, errno, strerror(errno)); sleep(100000); return 5;
  }
  if (!strcmp(mode, "spam")) { /* output cap */
    static char b[4096]; memset(b, 'A', sizeof b); for (int i = 0; i < 2048; i++) fwrite(b, 1, sizeof b, stdout); printf("\nTAIL-END\n"); return 0;
  }
  return 64;
}
