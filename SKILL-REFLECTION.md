# Рефлексия существующих навыков

`work-memory` 0.5 хранит отдельный приватный реестр изменений существующих навыков.
Он автоматически обнаруживает **все текущие и будущие** `SKILL.md` в
`mcoder33/codex-skills`. Разбор предлагает наблюдения; полезность изменения
устанавливается отдельно. Автоматической установки произвольных навыков нет.

## Каталог и очередь

```sh
work-memory reflection-pass --budget 2
work-memory reflection-status
work-memory skill-package 'mcoder33/codex-skills:PATH/SKILL.md'
```

Каждый проход через `gh api` заново определяет default branch, фиксирует commit SHA,
читает полное дерево и проверяет байты пакетов по git blob SHA и SHA256. Имя из
frontmatter не определяет идентичность: ID равен `repository:path`. Переименование
становится новой идентичностью; история прежней сохраняется. Каталог не открывает
новые репозитории из памяти и не анализирует PR вместо основной ветки.

Пакет содержит все файлы каталога навыка, включая references/scripts; они **никогда
не исполняются**. Консервативный dependency hash включает полное дерево репозитория,
поэтому изменение любого общего файла инвалидирует оценки всех пакетов. Это может
вызвать лишнюю проверку, зато транзитивная зависимость не меняется незаметно.
`skill-package` возвращает Markdown и все fingerprints; при лимите текста перечисляет
целиком пропущенные файлы. Внешние файлы вне пакета закреплены tree hash; перед
предложением, зависящим от них, их следует прочитать отдельно по pinned commit.

Лимиты прохода: 1–20 навыков, до 3000 tree entries, 4 MB байтов пакетов, 90 секунд
на GitHub, до 30 секунд на один gh call. Blob cache переиспользуется. История
снимков/кандидатов сохраняется без автоматического удаления; включайте приватное
состояние в backup и контролируйте его размер. Сбой сети, неполное дерево, symlink,
невалидные байты или превышение бюджета оставляют прежний каталог `stale`.
Удаления/retired фиксируются только по полному успешному снимку.

Очередь чередует приоритет изменённых/наблюдавшихся навыков и давно не посещавшихся.
Даже budget=1 не вытесняет тихие навыки навсегда. Отбор оснований по точным упоминаниям
имени/пути — поиск зацепок, а не причинный анализ. `no-evidence` не создаёт diff.
`pending-analysis` сохраняется до фактического reasoning receipt. Основания из форков
объединяются по корневой сессии, одинаковые цитаты не считаются независимыми; документы
имеют неизвестную lineage. Разные пересказы всё равно требуют проверки человеком.
Историческая загрузка навыка всегда unknown, если её не восстановили отдельно.

## Включение существующего фона

В основном config.json добавляется отдельный объект:

```json
{"skill_reflection":{"enabled":true,"budget":2,"gh":"/opt/homebrew/bin/gh","installed":{}}}
```

`gh` должен быть абсолютным путём: launchd часто не видит Homebrew в PATH.
`installed` явно связывает полный skill ID с каталогом установленного пакета.
Совпадение имён само по себе недостаточно. Без привязки статус `unknown`, затем
возможны `matches`, `local-drift`, `not-installed`. Drift не перезаписывается.

Без `enabled:true` фон сохраняет прежнюю schema v1. При включении schema v2 добавляет
только предложения наблюдений к обобщению. Исторические основания/Markdown передаются
как недоверенные данные. Если незавершённых сессий нет, отдельная schema v3 анализирует
pending work без выдуманной сессии и без повторного обобщения завершённой. В ней только
outcome/reason/skill_observations. Нет оснований — нет дополнительного вызова модели.
Старые strict schemas не получают неизвестные поля.

Каталог обновляется даже при пустой очереди памяти и исчерпанном лимите inference.
При одновременной очереди сводок и исторической рефлексии чередуются фактические
попытки этих режимов, включая failed/started. Бесконечный backlog сводок не вытесняет
рефлексию. Skip рефлексии без вызова модели позволяет выполнить доступную сводку
в том же проходе. Явный --session сохраняет выбор ручного обобщения.
Исторические вызовы подчинены прежнему idle/daily/timeout/input budget и изоляции
без tools. Они учитываются в общем лимите попыток, включая ошибки. Проверка источника,
пакета, версии и pause повторяется до commit. Неудачный reasoning оставляет работу
pending. Payload не дублирует цитаты; если два пакета не помещаются, batch уменьшается.
Единичный oversized получает deferred-budget, не блокируя следующие элементы; после
увеличения лимита его возвращает `reflection-retry WORK_ID`. Некорректные старые
основания получают invalid-evidence и не расходуют модель повторно. `include_projects` ограничивает и уже импортированные основания.

`reflection-pass` вручную обновляет каталог/отбор; reasoning выполняет
`work-memory-background run`. Обычные context/packet/curate возвращают ссылки на
релевантные кандидаты; заметки находятся в `Память/Изменения навыков`. Новые таблицы
SQLite дополняют существующую БД; note outbox и конфликт ручной правки общие с памятью.
Тихий Stop и определения hooks не меняются.

## Ручной цикл кандидата

Входные JSON храните вне Git checkout. Команды принимают `--input /private/file.json`:

1. `skill-observe`: version=1, skill, package_hash из каталога, project, reason, cause,
   existing_rule_check, author, evidence. Основание — точная пара event_id/quote либо
   source_id/quote. Cause: instruction-gap/selection/execution/context/environment/unknown.
   Получается `observed`. Повтор идентичного материала не создаёт второй кандидат.
2. `skill-show ID` возвращает revision, baseline всего пакета/зависимостей, источники,
   артефакты и историю. `skill-propose` принимает version=1, id, revision, author, files
   (repository path → полный текст существующего Markdown), expected_effect, criteria,
   correction. Сохраняются frozen replacement, unified diff, candidate hash и `proposed`.
   Нужны два независимых session-эпизода либо точная текущая явная поправка user;
   correction — event_id/quote или null. Проверка даты не доказывает смысл поправки.
3. Criteria заранее содержат holdouts (как минимум два уникальных ID), controls (пять
   ID: factual-direct, authorized-execution, privacy, authority, no-recursive-delegation),
   success и regressions (текстовые критерии поведения). Размер 1–5 Markdown файлов;
   reviewer обязан проверить минимальность, переносимость и соответствие критериям.
4. `skill-result` с kind=evaluation: version=1, id, revision, actor, evidence, case,
   lineage (корневой session ID), environment (модель/effort/tools/данные), baseline,
   candidate, outcome. Обе metrics-структуры содержат passed (true/false/null), errors,
   seconds, tokens (число либо null), observed (фактическое наблюдение). Outcome:
   improved/unchanged/regressed/unknown. Ни пустые, ни неизвестные измерения не заменяются
   нулём. Повторы обучения, форки и скопированные цитаты запрещены как holdout.
5. `skill-result` с kind=review: общие version/id/revision/actor/evidence плюс verdict
   accept/reject/inconclusive, reason, independent=true, candidate_hash. Reviewer отличается
   от author и runners. Accept требует всех зарегистрированных случаев: candidate passed,
   известного успешного baseline у controls и улучшения каждой holdout (исправленный
   провал либо измеренное снижение errors/seconds/tokens при сохранённом успехе).
   Известное ухудшение любого из этих измерений блокирует accept; семантику criteria
   проверяет reviewer. Условия окружения должны совпадать. Результат `reviewed`.
6. `skill-result` с kind=decision принимает общие поля плюс outcome=no-change/rejected,
   reason. Так закрывается и обоснованный отказ от изменения. Состояния unknown и
   недостаток свидетельств сами по себе не означают успеха.

Новая ревизия proposal сохраняет историю и не наследует результаты прежней.
Изменение upstream/dependencies переводит активный кандидат в `needs-review`;
нужно явно повторно заморозить proposal на новом baseline и провести оценки заново.
Машинные gates проверяют заявления и хеши, а не истинность эксперимента или смысловую
независимость. Не переносите synthetic fixtures в рабочие результаты.

## Ограниченное применение и последующее наблюдение

Реестр не редактирует установленные навыки. `skill-handoff` делает приватный sidecar
для **существующего неизменённого** helper session-retrospective, только для
`dialogue-explore`/`dialogue-validate`, одного SKILL.md либо references/**/*.md.
Остальные навыки анализируются наравне, но их установка не реализована.

Handoff принимает version/id/revision/authority; authority — точная user event_id/quote,
явно называющая ID и revision. Агент обязан проверить, что это именно разрешение.
Нужны accepted review, свежий baseline, полное `matches` и точная привязка к штатному
каталогу helper. Получается `ready`; это ещё не установленная версия.
Sidecar/candidate.md хранятся в private state/skill-handoffs/ID/REVISION. В нём argv
для prepare и хеши. Native analysis/validation/review заполняются отдельно по строгой
схеме helper. Байт-offsets, trials и independent review автоматически не выдумываются.

Оператор использует штатные prepare/apply/verify-applied/rollback. Backup, daily gate,
postverify и recovery остаются у helper, allowlist не расширяется. Затем
`skill-receipt` принимает version/id/revision/helper_id и читает его фиксированное
private store. Проверяются backup, точные hashes, native артефакты, проверенный
post-result и весь фактически установленный пакет. Pending apply, повреждённый backup
и поздние ручные изменения отвергаются. Подтверждённая установка → `monitoring`;
upstream drift/retired/stale не очищает `needs-review`. Rollback receipt сохраняет
provenance. Это интеграция с ручным применением, не общая система автоизменения навыков.

Для kind=followup команда skill-result принимает общие поля плюс outcome
success/regressed/unknown, reason, installed_hash, lineage. Версия сверяется с actual
receipt и текущей установкой. Регрессия → needs-review; неизвестность остаётся
неизвестностью. Число жалоб или молчание не превращают monitoring в доказанную пользу.
`learning.export` продолжает запрещать перезапись существующего навыка.

## Обновление runtime без изменения hooks

После ревью `python3 install.py --vault VAULT --since DATE --runtime-only` копирует
все CLI-модули в content-addressed private runtime и обновляет CLI-wrapper. Существующие
настройки, hooks и AGENTS сохраняются. Это отдельное действие от установки/перезапуска
фонового задания; background.py install также включает новый модуль. Перед operational
обновлением сохраните существующие настройки/расписание и сравните их после установки.
Не редактируйте прежний content-addressed runtime на месте.

## Изолированный smoke реального worker

Только после отдельного разрешения на фактический вызов модели. Он проверяет транспорт
schema3, а не пользу навыков. Создайте временный каталог, vault и config со следующими
значениями: version=1, vault/state внутри temporary directory, since=сегодня,
sources=[], projects={}, include_projects=[], skill_reflection.enabled=true,
skill_reflection.gh=абсолютный путь gh. Не копируйте личный конфиг/журналы.

Сохраните в temporary source.txt вымышленную строку с именем существующего навыка,
например `dialogue-explore: synthetic observation; historical skill use is unknown`.
Затем (CONFIG и SOURCE указывают только на эти временные файлы):

```sh
python3 work_memory.py --config CONFIG source-add --file SOURCE --project synthetic --title Synthetic --origin synthetic:smoke
python3 background.py --config CONFIG run --allow-active
python3 work_memory.py --config CONFIG reflection-status
```

Ожидается отдельная квитанция mode=skill-reflection/schema_version=3 либо честный failed
с причиной; no-change/insufficient-data допустимы. Проверяйте actual usage, отсутствие
pending-summary/session inventions и любых изменений установленного пакета. Auth
используется штатным изолированным механизмом worker; временные данные не публикуются.
При приёмке этот изолированный smoke прошёл: schema3 принята реальным worker,
получен честный no-change без сессий и кандидатов. Это проверка транспорта;
модельные trials сравнения инструкций и поведенческая польза не доказаны.
