# ANNA dev-loop conveniences.
#
# Targets:
#   dev-restart   — reinstall from the local source tree and restart the
#                   running systemd unit. The post-PATH-migration analogue
#                   of `cd ~/anna && git pull && systemctl --user restart anna`
#                   under the old `pip install -e .` model.
#   status        — short systemd status, head only.
#
# Phase A: Linux-only. Phase B extends with launchctl on macOS.

.PHONY: dev-restart status

dev-restart:
	uv tool install . --reinstall
	systemctl --user restart anna
	systemctl --user status anna --no-pager | head -8

status:
	systemctl --user status anna --no-pager | head -8
