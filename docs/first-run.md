# First run: start the server and create the owner

`nuramesh serve` starts with authentication **on**: every route needs a signed-in person or an API token. There is
no account yet on a new store, so the first thing to do is create the owner. Nothing here needs a key or the network.

## On your own machine (a terminal)

```sh
nuramesh serve my-team
```

When no owner has ever been recorded, the server is on loopback, you are at a terminal and the process is not in a
container, it prints **one line** with a setup link (it prints it once, and only in a terminal):

```text
http://127.0.0.1:8765/setup#token=...
```

1. Open the link in your browser. The token is in the part after `#`, which a browser never sends to a server, so it
   is in no log. The page removes it from the address bar at once.
2. Choose an email and a password (at least 12 characters) and submit. You are not signed in yet.
3. Sign in with that email and password in the app.

The link works **once** and for **15 minutes** (900 seconds). If it has expired, or you opened it twice, restart
`nuramesh serve` (the new link makes every earlier one dead) or run `nuramesh auth new-setup-token --team my-team`.

## In a container, behind a proxy, or without a terminal

The server prints no link there, only a hint: its output can be a container log, and a link in a log is a secret in a
log. Run, in a terminal on the server:

```sh
nuramesh auth new-setup-token --team my-team
```

It prints a one-time token once (valid 24 hours, works once). Paste it on the app's setup page, or open the app's
`/setup#token=<token>` address. See [self-hosting.md](self-hosting.md) for the Compose stack.

## `--auth off`

`nuramesh serve --auth off` is for looking around, and you must type it. It listens on loopback only, reads are open,
every change is refused (403 `authentication_required`), nobody can approve anything, and a banner says so. It cannot
be combined with a network address, `--allowed-host`, `--trusted-proxy` or a hosted deployment. No environment
variable or setting turns authentication off.

## An organization that was closed

If the only owner was erased, the organization is closed: no link and no token can be made, on purpose. Start a fresh
store.
