# systemd service 設定

TQark-web 用 user-level systemd,跟 OpenClaw 一樣(不用 sudo)。

## Service file
位置:`~/.config/systemd/user/tqark-web.service`

## 安裝步驟
```bash
# 1. 設定 lingering(讓 user session 結束後 service 還能跑)
loginctl enable-linger $USER

# 2. 載入 service file
systemctl --user daemon-reload

# 3. 啟動 + 開機自啟
systemctl --user enable --now tqark-web

# 4. 看狀態
systemctl --user status tqark-web
journalctl --user -u tqark-web -f
```

## 路徑
- WorkingDirectory:`/home/aping/MyProjects/TQark-web/backend`
- ExecStart:`/home/aping/MyProjects/TQark-web/backend/run.sh`
- EnvironmentFile:`/home/aping/MyProjects/TQark-web/credentials/.env`

## 重啟 / 停服務
```bash
systemctl --user restart tqark-web
systemctl --user stop tqark-web
```

## 看 log
```bash
journalctl --user -u tqark-web -f          # 即時
journalctl --user -u tqark-web -n 100      # 最近 100 行
```

