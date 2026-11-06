# Работа для второго тура

## Ссылки

- Готовый бот MAX: https://max.ru/aktivschool_bot
- Репозиторий GitHub: https://github.com/amaev8595-design/aktivschool_bot

## Содержимое репозитория

- `app/Dockerfile` — сборка Docker-образа приложения;
- `docker-compose.yml` — запуск приложения и PostgreSQL;
- `requirements.txt` — зависимости Python с версиями;
- `README.md` — инструкция по настройке, сборке и запуску;
- `.env.example` — пример переменных окружения без секретных значений.

## Быстрый запуск

```powershell
Copy-Item .env.example .env
notepad .env
docker compose build app
docker compose up -d
docker compose ps
```

После запуска откройте бота MAX и нажмите системную кнопку `Start`. Для
локального режима polling также поддерживается команда `/start`.
