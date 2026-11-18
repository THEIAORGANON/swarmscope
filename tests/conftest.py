"""Every git call in the tests ignores the user's global and system config: a commit.gpgsign = true with an
SSH or GPG key there would make each test commit ask for a passphrase (or fail), as on the lab Mac."""
import os

os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
