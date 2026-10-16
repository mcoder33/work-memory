# Проверка динамической рефлексии навыков

Контракт: анализировать текущие/будущие навыки согласованного GitHub-репозитория,
сохранять проверяемые предложения и реальный ручной lifecycle, не расширяя autoapply.
Baseline: память/процедуры, strict background schema1, тихий Stop и ограниченный helper.
Installed helper и определения hooks не менялись. После независимого ревью
выполнена отдельно разрешённая локальная активация существующего worker.

| Row | Область/обещание | Затронутые файлы | Доказательство / предел |
| --- | --- | --- | --- |
| R1 | Dynamic catalog, immutable SHA, full tree, dependencies, stale/retired, explicit installed identity | skill_reflection.py, tests/test_skill_reflection.py | Synthetic new/changed/removed/dependency, corrupt/truncated/network, drift, root live gh smoke |
| R2 | Отдельный реестр, точные sources, fork/content dedup, версии и история | skill_reflection.py, tests/test_skill_reflection.py | Scope/quote/hash failures, forks/copies, stale revisions; историческая загрузка unknown |
| R3 | Bounded fair queue и reasoning из исторических оснований | skill_reflection.py, background.py, tests/test_skill_reflection.py | budget1 starvation regression; persistent pending work; schema3 без pending session, failures/daily cap/source race |
| R4 | Proposal → evaluations → review → scoped handoff → receipt → followup | skill_reflection.py, tests/test_skill_reflection.py | Unknown/negative/NaN metrics, measured wins, independent review; native receipt/corrupt backup/pending/late edits/rollback; реальные trials не проводились |
| R5 | context/packet/curate/background/installation совместимы | work_memory.py, background.py, install.py, .github/workflows/ci.yml, tests/test_install.py, tests/test_skill_reflection.py | schema1 сохранена, schema2/3 отдельные, transactional extension, runtime-only, compile CI; остальные hooks/recovery tests |
| R6 | Проверяемая эксплуатация и честные ограничения | README.md, BACKGROUND.md, SKILL-REFLECTION.md, SKILL-REFLECTION-VERIFICATION.md | Ручные команды, config, scope применения и safe real-worker smoke описаны |

Проверки: `python3 -m unittest discover -s tests -v`,
`python3 -m py_compile work_memory.py memory_hooks.py learning.py skill_reflection.py background.py install.py`,
`git diff --check`. Полный итоговый результат фиксируется в отчёте реализации.

Итог перед независимой приёмкой: **125/125 tests прошли** (40 новых проверок
рефлексии и новый runtime-only test включены); py_compile и diff --check прошли.
Проверены также системные/injected старые pending entries, ограничение input batch,
deferred-budget без starvation, source snapshots, executable bit и idle guard. Отдельные регрессии проверяют
чередование attempted modes при непрерывной очереди сводок, failed reflection
и fallback к summary после reflection skip без дополнительного inference.

Root выполнил read-only live gh probe: default main,
commit c25cce9a533a306d8ade0b72ea5cf5b1f4867832, восемь обнаруженных навыков,
25 blob packages; последующий isolated DB smoke обнаружил девятый новый навык,
следующий refresh не сообщил изменений и продвинул очередь. Личные журналы не читались.
Это свидетельство технического обнаружения, не эффект инструкций.

Независимый reviewer принял implementation commit `8eb1af8`. Реальный изолированный
worker schema3 smoke прошёл: `status=applied`, `mode=skill-reflection`, outcome=no-change,
input tokens=13638, output tokens=118, reasoning tokens=0. Использован только вымышленный
source snapshot; sessions=0, candidates=0, временное состояние удалено. Личные журналы
и установленные навыки не использовались для этого smoke. Это транспортная проверка,
не поведенческий trial и не доказательство полезности инструкции.

Локальная активация: перед изменениями сохранены config, wrappers, background settings,
plist и согласованный SQLite backup. Включены reflection/budget2/абсолютный gh;
CLI и существующий curator используют независимые content-addressed runtime с новым
модулем. Settings enabled/idle/daily/model/effort/timeout/input сохранены; interval3600
и исходный config path сохранены. Hooks SHA256 до/после совпал. Сохранён guarded rollback,
который не восстанавливает SQLite поверх новых событий и не редактирует hooks/skills.

Installed CLI `reflection-pass --budget 2` прошёл: fresh catalog,
commit `f717f154bb71cc3de590b314f99a72825d4c8563`, 9 активных навыков, 0 retired,
2 selected skills, pending-analysis сохранён для будущего idle reasoning. Installed
background dry-run импортирует новый runtime/config, возвращает no-idle-pending-session
и не вызывает модель. Pending writes=0, issues=0; launchctl зарегистрирован на новый
runtime с прежним расписанием. Фактическое следующее срабатывание таймера ещё не наблюдалось.

Ограничения: keyword retrieval может пропустить эпизоды без имени навыка; semantic
independence и истинность авторских результатов требуют reviewer. Полное дерево
консервативно инвалидирует пакеты даже на нерелевантных изменениях. История и blob cache
сохраняются в приватной БД и требуют backup/контроля размера. Изменение инструкций
вне двух навыков прежнего helper остаётся предложением; действующие навыки не изменялись.
HTTP/UI routes отсутствуют: проверяется CLI и фоновый Python worker, веб-smoke неприменим.
