[Unit]
Description=Telegram Hongbao Bot (aiogram)
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu
EnvironmentFile=-/home/ubuntu/.env
ExecStart=/home/ubuntu/.venv/bin/python /home/ubuntu/app.py
KillSignal=SIGINT
TimeoutStopSec=20
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
