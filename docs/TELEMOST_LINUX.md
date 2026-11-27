# Учебный звонок в Яндекс Телемосте на Linux

Консольный клиент слушает по команде оператора, отвечает голосом из браузера и держит историю последних реплик в памяти до выхода. Комната, звук и облачные вызовы проверяются отдельно. Для этого теста подготовьте второго участника на другом устройстве и договоритесь, что учебные реплики передаются в SpeechKit и Yandex AI Studio.

## Подготовьте звук

Нужны настольная сессия Linux с PipeWire-Pulse или PulseAudio, браузер, команды `pactl`, `parec` и `pacat`, Python 3.12 или новее и зависимости из README. В Fedora команды аудио поставляются в пакете `pulseaudio-utils`, в Debian и Ubuntu — в `pulseaudio-utils`.

Выполните один раз на время звонка и сохраните переменные с идентификаторами модулей в этом терминале:

```bash
telemost_in_module=$(pactl load-module module-null-sink sink_name=telemost_in sink_properties=device.description=Telemost_In)
telemost_out_module=$(pactl load-module module-null-sink sink_name=telemost_out sink_properties=device.description=Telemost_Out)
telemost_mic_module=$(pactl load-module module-remap-source master=telemost_out.monitor source_name=telemost_mic source_properties=device.description=Telemost_Mic)
.venv/bin/python -m app.meetings --list-devices
```

Откройте ссылку на учебную комнату в браузере. Войдите как «ИИ-помощник». Если включена комната ожидания, попросите организатора вас впустить. Дайте браузеру доступ к микрофону и в настройках звонка выберите **динамики Telemost_In** и **микрофон Telemost_Mic**. Включите этот микрофон в звонке. Если браузер не даёт выбрать устройства, откройте `pavucontrol` во время звонка и перенаправьте поток воспроизведения браузера в `Telemost_In`, а поток записи браузера на `Telemost_Mic`. На время прогона закройте другие вкладки браузера со звуком. [Вход в звонок](https://www.yandex.ru/support/yandex-360/business/telemost/web/ru/call/join) · [Настройка камеры и звука](https://www.yandex.ru/support/yandex-360/business/telemost/web/ru/call/access)

Сначала проверьте микрофон без облачных ключей:

```bash
.venv/bin/python -m app.meetings --audio-check
```

Второй участник должен услышать короткий сигнал. У оператора звук собеседника направлен на виртуальное устройство. Слушайте звонок со второго устройства, если нужно контролировать разговор на слух.

## Запустите диалог

Создайте ключи сервисных аккаунтов в тестовом каталоге Yandex Cloud с активным доступом к API. В том же терминале задайте переменные процесса:

| Переменная | Доступ |
| --- | --- |
| `YANDEX_FOLDER_ID` | Идентификатор каталога модели |
| `YANDEX_LLM_API_KEY` | Роль `ai.languageModels.user`, область `yc.ai.foundationModels.execute` |
| `YANDEX_SPEECH_API_KEY` | Роли `ai.speechkit-stt.user` и `ai.speechkit-tts.user`, области `yc.ai.speechkitStt.execute` и `yc.ai.speechkitTts.execute` |
| `YANDEX_LLM_MODEL` | Необязательно, по умолчанию `yandexgpt-5.1` |

Ключи и приглашение храните локально. Для гостевого входа по ссылке отдельный ключ Телемоста не требуется. [Ключи и области доступа](https://yandex.cloud/ru/docs/iam/concepts/authorization/api-key)

```bash
.venv/bin/python -m app.meetings
```

1. После входа в комнату нажмите **Enter** для приветствия.
2. Нажмите **Enter** для начала захвата. По подсказке «Говорите» попросите участника произнести короткую фразу с неожиданной датой или деталью. По окончании фразы нажмите **Enter** ещё раз. Лимит захвата по умолчанию составляет 20 секунд. Его можно задать флагом `--max-seconds` в диапазоне от 1 до 30 секунд.
3. Дождитесь ответа и повторите с уточняющим вопросом. Сверьте смысл обоих ответов на втором устройстве. Консоль показывает время от конца захвата до запуска локального воспроизведения. Чтобы измерить задержку в самом звонке, засеките время от конца реплики до первого слышимого звука на втором устройстве.
4. Введите `q`, выйдите из комнаты в браузере и выгрузите созданные модули в обратном порядке.

```bash
pactl unload-module "$telemost_mic_module"
pactl unload-module "$telemost_out_module"
pactl unload-module "$telemost_in_module"
```

Если имена устройств отличаются, укажите их флагами `--source` и `--sink`. При пустом распознавании помощник просит повторить. Ошибку облачного запроса клиент выводит кодом, после чего можно продолжить диалог. Клиент не пишет звук, расшифровку или приглашение на диск. Режим с OBS, HTTP-сессией и передачей расшифровки описан в [контракте интеграции](integrations/meeting.md).
