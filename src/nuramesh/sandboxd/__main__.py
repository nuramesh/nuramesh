"""`python -m nuramesh.sandboxd`: run the supervisor until SIGTERM."""

import sys

from nuramesh.sandboxd.supervisor import main

if __name__ == "__main__":  # importing the module (the import-boundary walk) runs nothing
    sys.exit(main())
