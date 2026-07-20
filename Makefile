UNIT := winnow.service
SYSTEMD_USER_DIR := $(HOME)/.config/systemd/user
DATA_DIR := $(HOME)/.local/share/winnow

.PHONY: install-service uninstall-service

install-service:
	install -d $(SYSTEMD_USER_DIR)
	install -d $(DATA_DIR)
	install -m 644 packaging/$(UNIT) $(SYSTEMD_USER_DIR)/$(UNIT)
	systemctl --user daemon-reload
	systemctl --user enable --now $(UNIT)
	@echo "installed $(UNIT); follow logs with: journalctl --user -u winnow -f"

uninstall-service:
	-systemctl --user disable --now $(UNIT)
	rm -f $(SYSTEMD_USER_DIR)/$(UNIT)
	systemctl --user daemon-reload
