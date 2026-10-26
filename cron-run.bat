@echo off
rem tg-ops: крон-автопилот (прописка + догонки + холодные касания).
rem Пример задачи Task Scheduler (действие):
rem   Program:   C:\Users\Maxim\Desktop\projects\tg-ops\cron-run.bat
rem   Arguments: (пусто; или --dry-run для прогона без отправок)
rem   Start in:  C:\Users\Maxim\Desktop\projects\tg-ops
rem Состояние/логи: leads\cron-run.log, leads\cron-state.json
cd /d %~dp0
py cron-run.py %* >> leads\cron-run.log 2>&1
