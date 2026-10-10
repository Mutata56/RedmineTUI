# RedmineTUI

Терминальный клиент Redmine. Ходит напрямую в REST API и рисует заявки текстом
в терминале, так что читать тикеты, менять статус, списывать время и постить
SQL-комментарии можно без вкладки браузера.


## Скриншоты

Демонстрация на локальном стенде: заявки, имена и данные вымышлены.

![Активные заявки, сгруппированные по статусу](docs/screenshots/01-issue-list.png)

<details>
<summary>Поиск, карточка заявки, SQL, редактор и другие экраны</summary>

### Поиск по проекту

![Поиск по проекту](docs/screenshots/02-search.png)

### Фильтр по статусу

![Фильтр по статусу](docs/screenshots/03-status-filter.png)

### Встроенная справка

![Встроенная справка](docs/screenshots/04-help.png)

### Карточка заявки и SQL

![Карточка заявки и SQL](docs/screenshots/05-issue-card.png)

### История, внутренний комментарий и SQL

![История, внутренний комментарий и SQL](docs/screenshots/06-history-sql.png)

### Свёрнутые SQL-блоки

![Свёрнутые SQL-блоки](docs/screenshots/07-collapsed-sql.png)

### Редактор комментария и живой предпросмотр

![Редактор комментария и живой предпросмотр](docs/screenshots/08-comment-editor.png)

### Комментарий, статус и время в черновике

![Комментарий, статус и время в черновике](docs/screenshots/09-unsent-draft.png)

### Просмотр черновика перед подтверждением

![Просмотр черновика перед подтверждением](docs/screenshots/10-draft-review.png)

### Карточка в узком терминале

![Карточка в узком терминале](docs/screenshots/11-narrow-terminal.png)

### Ошибка подключения

![Ошибка подключения](docs/screenshots/12-api-error.png)

### Пустой список

![Пустой список](docs/screenshots/13-empty-state.png)

</details>

## Что внутри

- **redmine-view** - интерактивный TUI: список заявок плюс карточка со всей
  историей, вложениями и подсветкой SQL. Правки (коммент, статус, время) копятся
  в черновик и уходят разом. Справка и описание по клавише `?`.
- **redmine-shift** - собирает смену (твои заявки за сегодня и вчера) в буфер.
- **redmine-notify** - фоновый демон (systemd --user): опрашивает Redmine и шлёт
  уведомления с кликабельным «открыть заявку».
- **redmine-list / redmine-issue / redmine-open** — мелкие хелперы для меню и
  быстрых открытий заявки.
- **redmine_tui.py** — общий движок (REST-клиент, тема, клеточный рендер
  терминала, рендер заявок). **redmine_conf.py** — общий загрузчик конфига.

## Зависимости

Python 3, `jq`, `curl`, `secret-tool` (libsecret). Для демона уведомлений:
`python-gobject` и libnotify. Опционально: `chafa` (превью картинок в sixel,
лучше всего в терминале foot), `wl-clipboard`, `fuzzel`.

## Установка

```sh
git clone https://github.com/Mutata56/RedmineTUI.git ~/RedmineTUI
cd ~/RedmineTUI
./install.sh
```

`install.sh` делает симлинки `bin/*` в `~/.local/bin` и создаёт конфиги из
шаблонов в `config/` (существующие не трогает).

## Конфигурация

Всё, что специфично для  установки (хост, id проектов, имена клиентов),
лежит в `~/.config` и не коммитится. В репозитории только шаблоны `*.example`.

| Файл | Из шаблона | Что хранит |
|------|------------|------------|
| `~/.config/redmine-tui.conf` | `config/redmine-tui.conf.example` | базовый URL, activity id, источники списка |
| `~/.config/redmine-notify/config` | `config/redmine-notify.config.example` | url, источники, статусы, частота опроса |
| `~/.config/redmine-shift-ban.txt` | `config/redmine-shift-ban.txt.example` | токены фильтра смены, по одному в строке |

API-ключ в системном keyring:

```sh
secret-tool store --label='Redmine API' service redmine-notify
```

## Демон уведомлений

```sh
cp systemd/redmine-notify.service ~/.config/systemd/user/
systemctl --user enable --now redmine-notify
journalctl --user -u redmine-notify -f
```

## Лицензия

MIT, см. [LICENSE](LICENSE).
