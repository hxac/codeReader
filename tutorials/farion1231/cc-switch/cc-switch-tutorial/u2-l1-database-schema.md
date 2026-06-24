# SQLite 数据库与表结构

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 cc-switch 的「单一数据源（SSOT）」在磁盘上的物理落点——`~/.cc-switch/cc-switch.db` 是怎么算出来的。
- 画出 `providers`、`mcp_servers`、`prompts`、`skills` 这四张核心表的主键与关键字段，并能解释为什么 `providers` 用复合主键 `(id, app_type)`。
- 解释 `settings` 表为什么采用最朴素的 `key/value` 两列结构，以及它和「设备级偏好 `settings.json`」各管什么。
- 区分「核心表」和「派生表」，理解 `provider_health`、`usage_daily_rollups`、`proxy_request_logs` 这些派生表各自被谁写入、被谁读取。

本讲是 U2「数据存储与 SSOT 机制」的第一讲，只看**表结构本身**；数据库连接的并发安全（DAO 层）放在 u2-l2，从旧 JSON 到数据库的迁移放在 u2-l3。

## 2. 前置知识

在进入本讲前，你需要先建立两个概念（它们在 u1-l2、u1-l5 已讲过，这里只做一句话回顾）：

- **SSOT（Single Source of Truth，单一事实源）**：cc-switch 把所有「需要跨设备同步」的数据集中存进一个 SQLite 数据库，数据库就是唯一权威。任何一处改动都先落库，再由同步层写到各 CLI 工具的真实配置文件（Live 文件）。
- **app_type（应用类型）**：cc-switch 同时管理七种 AI CLI 工具，但**只有三种会经过本地代理**（claude / codex / gemini），所以代理相关的表会把 `app_type` 约束为这三者；而 provider / mcp / skills 等表则用更宽的字符串列。

另外两个术语在本讲会反复出现：

- **Live 文件**：各 CLI 工具真正读取的真实配置文件（如 `~/.claude/settings.json`）。数据库是 SSOT，Live 文件是「数据库内容的镜像」。
- **复合主键（composite primary key）**：用不止一列组合起来作为一行的唯一标识。本讲会看到 `providers` 用 `(id, app_type)` 作主键——同一个供应商 id 在不同工具下可以是两条独立记录。

## 3. 本讲源码地图

本讲涉及的关键文件都位于后端 `src-tauri/src/`：

| 文件 | 作用 |
| --- | --- |
| `database/mod.rs` | `Database` 结构体定义、`Database::init()` 初始化、`SCHEMA_VERSION` 常量、连接的 `Mutex` 包装。 |
| `database/schema.rs` | **本讲的主角**：所有 `CREATE TABLE` 语句、版本迁移函数（`migrate_v0_to_v1` … `migrate_v10_to_v11`）、`seed_model_pricing` 种子数据。 |
| `config.rs` | 路径计算：`get_app_config_dir()` 决定数据库文件落在哪个目录。 |
| `database/dao/settings.rs` | `settings` 键值表的读写实现，是理解「为什么用 key-value」的最佳样本。 |

阅读建议：先看 `config.rs` 里路径怎么算（知道库在哪），再看 `mod.rs` 的 `init()`（知道库怎么打开），最后精读 `schema.rs` 的 `create_tables_on_conn`（知道库里有哪些表）。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应规格里的四块：**数据库文件定位 → 核心表结构 → settings 键值表 → 派生表用途**。

### 4.1 数据库文件定位：SSOT 的物理落点

#### 4.1.1 概念说明

SSOT 是个抽象原则，落到工程上必须回答一个问题：「这个唯一数据源，到底是磁盘上的哪个文件？」

cc-switch 的答案是：用户主目录下的 `~/.cc-switch/cc-switch.db`。这个路径由两段拼出来——

1. **目录**：`get_app_config_dir()` → 默认 `~/.cc-switch`。
2. **文件名**：`Database::init()` 再 `.join("cc-switch.db")`。

注意区分三个容易混淆的路径：

| 路径 | 内容 | 是否参与云同步 |
| --- | --- | --- |
| `~/.cc-switch/cc-switch.db` | **本讲的 SSOT 数据库**，所有 provider/mcp/skills/用量 | 是（数据库整库同步） |
| `~/.cc-switch/config.json` | 旧版配置文件，现已迁移入库，仅留作历史 | 否 |
| 设备级 `settings.json`（语言、主题、开机自启等） | 设备偏好，因设备而异 | **否**（不该跨设备同步） |

「数据库进 SSOT、设备偏好进 settings.json」正是 u1-l2 讲过的**双层存储**——本讲的 `settings` 表（数据库内）和这里的 `settings.json`（文件）名字相近但职责不同，下文 4.3 会专门辨析。

#### 4.1.2 核心流程

数据库文件定位的执行顺序是：

1. Tauri `setup` 钩子调用 `Database::init()`。
2. `init()` 调 `get_app_config_dir()` 得到目录，再拼上 `cc-switch.db`。
3. 若父目录不存在则 `create_dir_all` 创建。
4. 用 `rusqlite::Connection::open` 打开（不存在则新建）。
5. 启用外键、（新库）配置增量 auto-vacuum，然后建表。

其中目录计算有一个**跨平台回退**逻辑值得记住：在 Windows 上，如果默认位置没有数据库，但 `$HOME/.cc-switch/cc-switch.db` 存在（v3.10.3 在被第三方工具注入的 `HOME` 下写过库），会回退到旧位置，避免「供应商消失」的回归。这是路径计算里唯一一处需要兼容历史的位置。

#### 4.1.3 源码精读

目录计算的根，`get_app_config_dir()` 默认返回 `~/.cc-switch`：

[config.rs:183-216](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/config.rs#L183-L216) —— `default_dir = get_home_dir().join(".cc-switch")`，并在 Windows 上对 v3.10.3 旧库做回退探测。

`Database::init()` 把目录和文件名拼起来，并完成打开、外键、建表：

[database/mod.rs:96-121](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/mod.rs#L96-L121) —— 关键两行：`let db_path = get_app_config_dir().join("cc-switch.db");` 与 `Connection::open(&db_path)`，随后 `PRAGMA foreign_keys = ON;`。

`get_home_dir()` 自身也有一层测试覆盖逻辑（`CC_SWITCH_TEST_HOME` 覆盖），但生产路径就是系统真实用户目录：

[config.rs:22-34](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/config.rs#L22-L34) —— 故意**不**直接读 `HOME` 环境变量，避免 Git/Cygwin 注入的 `HOME` 让库跑到非预期路径。

#### 4.1.4 代码实践

1. **目标**：确认你这台机器上数据库文件的完整绝对路径。
2. **操作步骤**：
   - 打开 `database/mod.rs`，定位 `Database::init`。
   - 用本机主目录替换占位符，写出完整路径（Linux/macOS：`/home/<你>/.cc-switch/cc-switch.db`；Windows：`C:\Users\<你>\.cc-switch\cc-switch.db`）。
   - （可选）若你已构建并运行过 cc-switch，用 `sqlite3` 打开它：`sqlite3 ~/.cc-switch/cc-switch.db ".tables"`。若未安装 `sqlite3` 或从未运行过应用，这一步写「待本地验证」。
3. **需要观察的现象**：`.tables` 应列出本讲后续讲到的 `providers`、`mcp_servers`、`prompts`、`skills`、`settings`、`proxy_config` 等表名。
4. **预期结果**：能写出与本机一致的绝对路径；若有库文件，表清单与第 4.2 节对得上。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_home_dir()` 不直接 `std::env::var("HOME")`？

**参考答案**：在 Windows 上 `HOME` 可能被 Git Bash / Cygwin / MSYS 等工具注入成一个与真实用户目录不同的值，导致 `.cc-switch/cc-switch.db` 落到意外位置，表现为「供应商/数据丢失」。代码改用 `dirs::home_dir()`（Windows 上走 `SHGetKnownFolderPath`），仅在默认位置无库时才回退探测 `HOME`。

**练习 2**：`config.json` 和 `cc-switch.db` 都在 `~/.cc-switch/` 下，谁是当前 SSOT？

**参考答案**：`cc-switch.db`。`config.json` 是旧版遗留，已由迁移逻辑（u2-l3）导入数据库，不再作为权威数据源。

### 4.2 核心表结构：providers / mcp_servers / prompts / skills

#### 4.2.1 概念说明

这四张表承载了 cc-switch 最核心的领域数据，对应四个功能面板：

- `providers` —— 供应商（最核心，一切切换都围绕它）。
- `mcp_servers` —— MCP 服务器统一清单。
- `prompts` —— 跨应用提示词（CLAUDE.md / AGENTS.md / GEMINI.md）。
- `skills` —— Skills 统一清单（v3.10.0+ 统一管理架构）。

它们有一个共同设计：**用一列 JSON 字符串存「异构/可变」的负载**，而把「需要查询/排序/建索引」的字段提成独立列。比如 `providers.settings_config` 是一整段 JSON（因为不同工具配置结构差异巨大），而 `is_current`、`sort_index` 是独立列（因为要按它们查询、排序）。

另一个关键点是主键策略的两种风格：

- **`providers` 用复合主键 `(id, app_type)`**：同一个供应商 id（如官方内置的某个 id）在不同工具下是不同记录，互不影响。
- **`mcp_servers` / `skills` 用单列主键 `id`**：一份 MCP / 一份 Skill 是全局唯一资源，再用 `enabled_claude` / `enabled_codex` … 一串布尔列表达「分发到哪些工具」。

#### 4.2.2 核心流程

所有表的创建都汇聚在一个函数 `create_tables_on_conn` 里，按编号顺序执行一连串 `CREATE TABLE IF NOT EXISTS`。它具有**幂等性**（`IF NOT EXISTS`），所以无论是全新库还是老库都会安全跑一遍。建表之后，`apply_schema_migrations` 再把老库的 `user_version` 一步步升到当前 `SCHEMA_VERSION`（迁移细节在 u2-l3）。

理解表结构，抓住三条线索即可：

1. **主键**：这行靠什么唯一标识？
2. **JSON 负载列**：哪一列吞下了「因工具而异」的可变内容？
3. **启用标志列**：`enabled_*` 这串布尔，表达「这个资源在哪些工具里生效」。

#### 4.2.3 源码精读

**`providers` 表**（复合主键 + JSON 负载 + 排序/当前标志）：

[database/schema.rs:27-43](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs#L27-L43) —— `PRIMARY KEY (id, app_type)`，`settings_config TEXT NOT NULL` 是 JSON 负载，`is_current` 标记当前激活供应商，`sort_index` 支持拖拽排序，`in_failover_queue` 标记是否进入故障转移队列。

`providers` 主要字段速查：

| 字段 | 含义 |
| --- | --- |
| `id` + `app_type` | 复合主键 |
| `name` | 供应商显示名 |
| `settings_config` | JSON 负载：真实写入各工具 Live 文件的配置内容 |
| `website_url` / `category` / `icon` / `icon_color` | 展示元数据 |
| `meta` | JSON：扩展元信息（如 usage_script、provider_type 等） |
| `is_current` | 是否当前激活（切换供应商的核心标志） |
| `sort_index` | 列表排序 |
| `in_failover_queue` | 是否在故障转移队列中（代理模块用） |

**`mcp_servers` 表**（单主键 + 五个启用布尔）：

[database/schema.rs:64-70](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs#L64-L70) —— `id TEXT PRIMARY KEY`，`server_config` 是 JSON 负载，`enabled_claude/codex/gemini/opencode/hermes` 五个布尔表达分发到哪些工具，`tags` 默认 `'[]'`。

**`skills` 表**（v3.10.0+ 统一结构，id 主键 + 仓库溯源字段 + 哈希）：

[database/schema.rs:84-101](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs#L84-L101) —— `id TEXT PRIMARY KEY`，`repo_owner/repo_name/repo_branch/readme_url` 记录 GitHub 仓库溯源，`content_hash` + `updated_at` 用于更新检测，同样有五个 `enabled_*` 布尔。

`skills` 主要字段速查：

| 字段 | 含义 |
| --- | --- |
| `id` | 主键（统一资源 id） |
| `name` / `description` | 展示信息 |
| `directory` | 统一存储目录 `~/.cc-switch/skills/<...>` |
| `repo_owner` / `repo_name` / `repo_branch` | GitHub 仓库溯源 |
| `enabled_claude` … `enabled_hermes` | 分发到哪些工具 |
| `content_hash` / `updated_at` | 更新检测（u6-l3） |

DAO 层读取 skills 时，正是把五个 `enabled_*` 列映射进一个 `SkillApps` 结构，可见「表结构 ↔ 业务结构」是一一对应的：

[database/dao/skills.rs:24-48](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/dao/skills.rs#L24-L48) —— `SELECT ... enabled_claude, enabled_codex, ...` 映射到 `SkillApps { claude, codex, gemini, opencode, hermes }`。

**`prompts` 表**：

[database/schema.rs:76-80](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs#L76-L80) —— 复合主键 `(id, app_type)`，`content` 存 Markdown 正文，`enabled` 标记是否激活。

#### 4.2.4 代码实践

1. **目标**：把 `providers`、`mcp_servers`、`skills` 三张表的「主键 + 关键字段」整理成一张图。
2. **操作步骤**：
   - 打开 `schema.rs`，分别找到这三张表的 `CREATE TABLE` 语句。
   - 为每张表列出：主键、JSON 负载列、启用标志列、排序/时间列。
   - 思考：`providers` 为什么不能像 `mcp_servers` 那样用单列 `id` 主键？
3. **需要观察的现象**：三张表的主键策略并不一致——`providers` 是 `(id, app_type)`，另两张是单 `id`。
4. **预期结果**：能口述「同一供应商 id 在 claude/codex/gemini 下可能是不同记录，所以必须用复合主键；而一个 MCP / Skill 是全局唯一资源，靠 `enabled_*` 决定分发范围」。

#### 4.2.5 小练习与答案

**练习 1**：`providers.settings_config` 为什么用一列 `TEXT` 存 JSON，而不是给每个配置项建一列？

**参考答案**：因为七种工具的配置结构差异巨大（JSON/TOML/YAML/JSON5，字段各不相同），把可变内容塞进固定列既不现实也无意义；只有需要查询/排序/建索引的维度（`is_current`、`sort_index`、`app_type`）才提成独立列，其余整段存 JSON。这是「关系型表 + JSON 负载列」的典型折中。

**练习 2**：`skills` 表的 `content_hash` 和 `updated_at` 是给谁用的？

**参考答案**：给「Skills 更新检测」（u6-l3）用——通过比较内容哈希判断远端仓库是否已更新，避免重复下载。

### 4.3 settings 键值表：为什么用 key-value

#### 4.3.1 概念说明

`settings` 表只有两列：`key TEXT PRIMARY KEY, value TEXT`。这是最朴素的键值结构。问题是：cc-switch 明明有专门的结构化表，为什么还要留这么一张「大杂烩」表？

答案是：**settings 表专门收纳「零散、异构、低频、不需要复杂查询」的配置**。它存的东西五花八门：

- **一次性启动 flag**：如 `official_providers_seeded`（官方供应商是否已播种）、`first_run_notice_shown`、`skills_ssot_migration_pending`。
- **以 JSON 存的结构化小配置**：如 `rectifier_config`、`optimizer_config`、`log_config`（整段 JSON 序列化后塞进 value）。
- **通用配置片段**：`common_config_<app_type>`（共享给各工具的公共配置，详见 u3-l4）。
- **全局出站代理**：`global_proxy_url`。

如果给这些每一项都建一张结构化表，会产生大量「只有一两行」的小表，维护成本远高于收益。键值表用「统一的 get/set 接口」消化掉这种异构性：新增一个配置项**不需要改表结构**，只需约定一个新 key。

> 注意辨析：数据库里的 `settings` **表**（参与云同步的零散配置）和文件系统里的设备级 `settings.json`（语言/主题/开机自启，**不**同步）是两回事。后者在 u9-l4 讲。

#### 4.3.2 核心流程

读写 `settings` 表的入口只有两个原语：

- `get_setting(key) -> Option<String>`：`SELECT value FROM settings WHERE key = ?`。
- `set_setting(key, value)`：`INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)`。

所有上层语义（flag、JSON 配置、片段）都建立在这两个原语之上。比如 `get_bool_flag` 就是「`get_setting` 后判断值是否为 `"true"` 或 `"1"`」；`get_rectifier_config` 就是「`get_setting("rectifier_config")` 后 `serde_json::from_str` 反序列化」。

这种「薄原语 + 上层语义包装」的好处是：**新增配置项零成本**——你不需要写迁移、不需要 `ALTER TABLE`，只要约定一个 key 名并选一种编码（字符串 / JSON）即可。

#### 4.3.3 源码精读

表定义仅一行：

[database/schema.rs:116-121](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs#L116-L121) —— `CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)`。

两个原语读写：

[database/dao/settings.rs:17-57](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/dao/settings.rs#L17-L57) —— `get_setting` 用 `SELECT value ... WHERE key = ?1`，`set_setting` 用 `INSERT OR REPLACE`，对应键值表的「读一个 / 写或覆盖一个」。

「以 JSON 存结构化小配置」的典型封装（整流器配置）：

[database/dao/settings.rs:244-260](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/dao/settings.rs#L244-L260) —— `get_rectifier_config` 取出字符串后 `serde_json::from_str`，缺失则返回默认值；`set_rectifier_config` 反向序列化后 `set_setting`。`optimizer_config`、`log_config` 完全同构。

「以 flag 存一次性信号」的封装：

[database/dao/settings.rs:41-46](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/dao/settings.rs#L41-L46) —— `get_bool_flag` 把字符串语义化成布尔，兼容 `"true"` 与历史写法 `"1"`。

#### 4.3.4 代码实践

1. **目标**：亲手用键值原语「发明」一个新配置项，验证零成本扩展。
2. **操作步骤**：
   - 在 `dao/settings.rs` 里观察 `get_setting` / `set_setting` 两个原语。
   - 假设你要加一个「是否开启某实验功能」的开关，写出它只需两步：约定 key 名（如 `feature_x_enabled`），调用 `db.set_setting("feature_x_enabled", "true")` / `db.get_bool_flag("feature_x_enabled")`。
   - 对照：如果改用结构化表，你需要改哪几处？（建表语句、DAO、迁移……）
3. **需要观察的现象**：键值方案下你**不碰**任何 `CREATE TABLE` 或 `ALTER TABLE`，也不碰 `schema.rs`。
4. **预期结果**：能用一句话说明「键值表的价值 = 用统一 get/set 消化异构、低频配置，新增项零迁移成本」。

> 说明：本实践是「源码阅读 + 设计推演」型，不要求改源码或运行；如你确实想验证，可在 `database/tests.rs` 风格的内存库测试里调用这两个原语观察写入与读回（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`set_setting` 用的是 `INSERT OR REPLACE`，这等价于什么语义？

**参考答案**：等价于「upsert」——key 不存在则插入，已存在则覆盖 value。因为 `key` 是主键，重复 key 会触发冲突，`OR REPLACE` 把旧行删掉再插入新行，实现「写或更新」。

**练习 2**：`optimizer_config` 这种结构化配置为什么也能塞进键值表？

**参考答案**：因为它整体作为一个「值」读写（取出来反序列化、写回去序列化），从不需要按内部字段做 SQL 查询/过滤；键值表只关心 key 级别的存取，对 value 内部结构无感，所以序列化成 JSON 后整段存即可。

### 4.4 派生表用途：proxy_config / provider_health / usage_daily_rollups

#### 4.4.1 概念说明

除了 4.2 的领域核心表，库里还有一批「派生表」——它们不是用户直接编辑的领域对象，而是由某个子系统**派生/聚合/记录**出来的数据。理解它们的关键是问两个问题：**谁写入它？谁读取它？**

| 派生表 | 谁写入 | 谁读取 | 用途 |
| --- | --- | --- | --- |
| `proxy_config` | 代理设置面板 | 代理服务器（U7） | 每应用的代理参数（三行结构） |
| `provider_health` | 代理健康检查 | 熔断器/故障转移（U7） | 供应商健康状态快照 |
| `proxy_request_logs` | 代理转发链路 | 用量统计（U8） | 每次请求的明细流水 |
| `usage_daily_rollups` | 定时 rollup 任务 | 用量仪表盘（U8） | 按天预聚合，加速趋势查询 |
| `model_pricing` | 启动 seed | 用量计价（U8） | 模型定价参考表 |
| `session_log_sync` | 会话用量同步 | 会话管理（U8） | 本地日志同步断点续传 |

派生表有两个值得记住的特征：

1. **外键级联**：`provider_health`、`provider_endpoints` 通过 `FOREIGN KEY ... ON DELETE CASCADE` 挂在 `providers` 上——删掉一个供应商，它的健康记录、端点记录自动消失。
2. **预聚合**：`usage_daily_rollups` 是「明细表 `proxy_request_logs` 的按天汇总」，目的是让仪表盘的按天趋势查询不必扫全量明细。

#### 4.4.2 核心流程

以 `proxy_config` 为例，它有一个特殊的「**三行结构**」：主键 `app_type` 被 `CHECK` 约束限定为 `'claude' / 'codex' / 'gemini'` 三者之一，所以这张表**永远只有三行**，每行是一套独立的代理参数（重试次数、各类超时、熔断阈值……）。建表时还会用 `INSERT OR IGNORE` 给这三行塞入各自的默认值。

以 `usage_daily_rollups` 为例，启动时 `Database::init` 会调用 `rollup_and_prune(30)`：把超过 30 天的明细聚合进日表、再删掉过期明细，并用 `PRAGMA incremental_vacuum` 回收空间。这就是「派生预聚合」的运转方式。

> 一个进阶细节：`usage_daily_rollups` 的主键在 v10→v11 迁移里被重建过，加入了 `request_model` 和 `pricing_model` 两个维度。原因是在「路由接管」模式下，客户端请求的模型别名（request_model）和上游真实计价模型（pricing_model）会分叉；如果只按 model 聚合，明细被 prune 后映射关系永久丢失、计费不可审计。

#### 4.4.3 源码精读

**`proxy_config` 三行结构**（CHECK 约束 + 大量默认值）：

[database/schema.rs:124-137](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs#L124-L137) —— `app_type TEXT PRIMARY KEY CHECK (app_type IN ('claude','codex','gemini'))`，`listen_port ... DEFAULT 15721`，含一整套熔断参数默认值。

三行默认数据的 seed：

[database/schema.rs:144-172](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs#L144-L172) —— 仅当表已有 `app_type` 列时，分别给 claude / codex / gemini `INSERT OR IGNORE` 各自默认参数。

**`provider_health` 外键级联**：

[database/schema.rs:175-181](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs#L175-L181) —— `FOREIGN KEY (provider_id, app_type) REFERENCES providers(id, app_type) ON DELETE CASCADE`，删供应商时健康记录自动清除。

**`usage_daily_rollups` 多维主键**（含 request_model/pricing_model 维度）：

[database/schema.rs:265-284](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs#L265-L284) —— 主键 `(date, app_type, provider_id, model, request_model, pricing_model)`，存各 token 计数与 `total_cost_usd`、`avg_latency_ms`。

**`proxy_request_logs` 明细表 + 多索引**：

[database/schema.rs:186-219](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/schema.rs#L186-L219) —— 一次请求一行，含 token/成本/延迟/状态码，并建了 provider、created_at、model、session、status 等多个索引加速统计查询。

启动时的 rollup + prune + vacuum（派生预聚合的触发点）：

[database/mod.rs:145-157](https://github.com/farion1231/cc-switch/blob/55abd1822c7d9a8b6f42aaf86b7020ab36ce0d9a/src-tauri/src/database/mod.rs#L145-L157) —— `cleanup_old_stream_check_logs(7)`、`rollup_and_prune(30)`、`PRAGMA incremental_vacuum`。

#### 4.4.4 代码实践

1. **目标**：用「谁写入 / 谁读取」的视角，给派生表归类。
2. **操作步骤**：
   - 在 `schema.rs` 中找到 `proxy_config`、`provider_health`、`usage_daily_rollups`、`proxy_request_logs` 四张表。
   - 对每张表标注：它的写入方属于哪个单元（U7 代理 / U8 用量），读取方属于哪个面板。
   - 重点看 `provider_health` 和 `provider_endpoints` 的 `ON DELETE CASCADE`，思考为什么需要级联。
3. **需要观察的现象**：派生表普遍带默认值（`DEFAULT ...`）和外键/索引，而 4.2 的领域核心表更强调主键与 JSON 负载。
4. **预期结果**：能说清「删一个 provider，它的 health/endpoint 记录会因级联自动消失；而用量明细一旦 prune，就只能靠 rollup 表继续提供历史趋势」。

#### 4.4.5 小练习与答案

**练习 1**：`proxy_config` 表为什么用 `CHECK (app_type IN ('claude','codex','gemini'))` 而不是像 `providers` 那样接受任意 `app_type`？

**参考答案**：因为本地代理只接管 claude/codex/gemini 三种工具（其余工具不经过代理），代理配置只对这三者有意义；CHECK 约束在数据库层挡住非法值，保证这张表恒为三行。

**练习 2**：为什么需要 `usage_daily_rollups`，不能直接对 `proxy_request_logs` 做趋势查询？

**参考答案**：明细表会随时间无限增长，按天聚合的全表扫描代价越来越高；rollup 表把每天每模型的数据预聚合好，仪表盘按天查趋势时只扫极少的聚合行。同时明细会定期 prune（`rollup_and_prune(30)`）， prune 之后历史趋势完全依赖 rollup 表，所以它必须先于 prune 写好。

## 5. 综合实践

**任务**：以「全新安装后第一次启动」为时间线，把本讲四块知识串起来，画出一张「建表 + 定位 + 写入」的全景。

请按以下步骤完成：

1. **定位**：写出全新安装时数据库文件的绝对路径（依据 `config.rs` 的 `get_app_config_dir` + `mod.rs` 的 `Database::init`）。
2. **建表**：在 `schema.rs` 的 `create_tables_on_conn` 中，按出现顺序列出四张核心表（providers / mcp_servers / prompts / skills）与四张派生表（proxy_config / provider_health / proxy_request_logs / usage_daily_rollups）。
3. **种子**：指出建表阶段就被写入种子的两张表——`proxy_config`（三行默认值）和 `model_pricing`（在 `init` 末尾由 `ensure_model_pricing_seeded` 写入）。给出 `seed_model_pricing` 与 `ensure_model_pricing_seeded` 的代码位置。
4. **辨析**：用一段话说明 `settings` 键值表与文件系统 `settings.json` 的分工——前者参与云同步、收纳零散配置；后者是设备级偏好、不同步。
5. **验证（可选）**：若本机有构建产物，运行应用一次后用 `sqlite3 ~/.cc-switch/cc-switch.db ".tables"` 与 `".schema providers"` 核对。无构建环境则全标「待本地验证」。

预期产出：一张包含「文件路径 → 表清单 → 种子表 → settings 分工」的简表或流程图，能口头复述 SSOT 在磁盘上的完整落点。

## 6. 本讲小结

- cc-switch 的 SSOT 物理落点是 `~/.cc-switch/cc-switch.db`，由 `get_app_config_dir()` + `Database::init()` 拼出；`config.json` 是旧版遗留，已迁移入库。
- `Database` 用 `Mutex<Connection>` 包装以支持 Tauri 多线程共享，建表集中在 `create_tables_on_conn`，幂等且带外键与索引。
- 四张核心表里，`providers` 用复合主键 `(id, app_type)` + JSON 负载列 `settings_config`；`mcp_servers` / `skills` 用单 `id` 主键 + 一串 `enabled_*` 布尔表达分发范围。
- `settings` 是两列键值表，专门收纳零散、异构、低频配置，新增项零迁移成本；它与设备级 `settings.json` 同名但职责不同。
- 派生表由子系统写入：`proxy_config` 恒为三行（claude/codex/gemini）、`provider_health` 经外键级联挂在 providers 上、`usage_daily_rollups` 是明细的按天预聚合。

## 7. 下一步学习建议

- **u2-l2 Database 连接管理与 DAO 层**：本讲只看了表「长什么样」，下一讲看「怎么并发安全地读写」——`Mutex<Connection>` 如何避免竞争，以及 `database/dao/` 如何按表提供查询函数。
- **u2-l3 数据迁移**：本讲多次提到 `SCHEMA_VERSION` 和 `migrate_v*`，迁移的完整故事（旧 JSON → 数据库、user_version 递增、失败回退）在那里讲透。
- **横向对照**：读 `database/dao/providers.rs`、`dao/mcp.rs`、`dao/skills.rs` 的 `SELECT` 语句，验证你在这讲画出的字段图是否与真实查询一致。
