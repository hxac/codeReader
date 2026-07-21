# 部署任务族

## 1. 本讲目标

在上一篇 u9-l1 中，我们只拆解了部署层的**调度骨架**——`DeploymentTask` 抽象基类与 `Deployer` 任务队列。本篇接着把骨架上跑的**具体任务**逐个讲透。读完本讲，你应当能够：

1. 说出 `levers` 模块注册的 11 个部署任务各自的名字与职责。
2. 理解「改动检测 → 配置编译 → 方案/词典构建 → 用户词典维护 → 清理」这条完整 deploy 链路上，每一步依赖前一步的什么产物（例如 `SchemaUpdate` 依赖 `ConfigFileUpdate` 产出的编译后配置，再依赖 `DictCompiler` 产出的 `.bin`）。
3. 区分**运行期维护任务**（`workspace_update` 等）与**安装期一次性任务**（`prebuild_all_schemas` / `symlinking_prebuilt_dictionaries`）的不同用途。
4. 看懂 `levers_module.cc` 是如何用一行 `new Component<XxxTask>` 把每个任务挂进 `Registry`，以及 C API 的 `deploy_workspace` / `start_maintenance` / `sync_user_data` 等入口如何通过名字把它们调度起来。

## 2. 前置知识

本讲建立在 u9-l1 已建立的认知之上，默认你已经理解：

- **`DeploymentTask` 基类契约**：唯一纯虚函数 `Run(Deployer*) -> bool`，通过 `Class<DeploymentTask, TaskInitializer>` 复用组件注册体系，以 `TaskInitializer`（即 `std::any`）擦除构造参数类型。
- **`Deployer` 的两种调度方式**：`RunTask` 同步执行（任务用 `unique_ptr` 独占、跑完即销毁），`ScheduleTask` 入队异步执行（任务用 `shared_ptr` 跨线程，在维护线程里跑）。
- **维护模式**：`StartMaintenance()` 期间 `Service::disabled()` 为真，前端无法创建会话。

此外需要了解（来自 u4 配置系统、u8 词典系统）：

- **配置的两条加载路径**：`config` 组件（运行时直接读 YAML）与 `config_builder` 组件（部署期经 `ConfigCompiler` 支持 `__include`/`__patch` DSL，并写 `__build_info`）。`ConfigFileUpdate` 正是在二者之间切换。
- **`DictCompiler`**：把人类可读的 `*.dict.yaml` 编译成 `.table.bin` / `.prism.bin` / `.reverse.bin` 三类二进制产物，校验和（checksum）驱动增量重建。
- **`ResourceResolver`**：用 prefix/suffix/root_path 在 `config_id` 与磁盘路径之间互译（u4-l2）。

一个贯穿全讲的直觉：**部署就是把「人类写的源文件」变成「引擎高效加载的二进制映像」，并在过程中做改动检测、版本兼容与垃圾回收。** 每个部署任务就是这条流水线上的一个工位。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/rime/lever/deployment_tasks.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.h) | 声明 11 个 `DeploymentTask` 子类，是本讲的「菜单」。 |
| [src/rime/lever/deployment_tasks.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc) | 11 个任务的 `Run()` 实现，是本讲精读重点。 |
| [src/rime/lever/levers_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/levers_module.cc) | 在 `levers` 模块初始化期把 11 个任务注册进 `Registry`。 |
| [src/rime_api_impl.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h) | C API 入口（`deploy_workspace`/`start_maintenance`/`sync_user_data` 等）调用 `RunTask`/`ScheduleTask`。 |
| [src/rime/deployer.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc) | `RunTask`/`ScheduleTask`/`Run` 的调度实现（u9-l1 已讲，本讲引用）。 |
| [src/rime/setup.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc) | 定义 `deployer` 模块组 = core + dict + levers，确保 `levers` 在部署期被加载。 |

## 4. 核心概念与源码讲解

本讲把 11 个任务拆成四个最小模块：① 注册全景；② 守门任务（改动检测与安装信息）；③ 核心编译链（配置编译、方案/词典构建、工作区编排）；④ 安装期与辅助任务族。

### 4.1 注册全景：levers_module 把任务族注册为组件

#### 4.1.1 概念说明

11 个部署任务都是 `DeploymentTask` 的子类，但它们并非硬编码在引擎里，而是像 Processor/Translator 一样**按名字注册进 `Registry`**。这样做的好处是：引擎核心不必知道「部署时该干哪些事」，只需在需要时 `DeploymentTask::Require("workspace_update")` 按名取货。`levers` 模块就是这批任务的「打包注册单元」——它属于 `deployer` 模块组（`core + dict + levers`），只有在部署期才会被加载，所以这些任务平时不占注册表。

#### 4.1.2 核心流程

```
RimeInitialize / LoadModules(kDeployerModules)
        │
        ▼
deployer 模块组.initialize  →  依次 core / dict / levers
        │
        ▼
levers 模块的 rime_levers_initialize()
        │  遍历 11 个任务，逐行 r.Register(name, new Component<Task>)
        ▼
Registry 持有 { "workspace_update" -> Component<WorkspaceUpdate>, ... }
        │
        ▼
此后任意线程可 DeploymentTask::Require(name)->Create(arg)->Run(deployer)
```

关键点：注册的是**工厂**（`Component<Task>`），不是任务实例。同一个任务名可被反复 `Create` 出多个实例分别 `Run`，这正是 `WorkspaceUpdate` 内部能 `new SchemaUpdate(path)` 多次实例化的前提。

#### 4.1.3 源码精读

`levers` 模块的初始化函数把 11 个任务依次注册：

[levers_module.cc:15-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/levers_module.cc#L15-L31) — `rime_levers_initialize()` 用 `Registry::Register(name, new Component<XxxTask>)` 把每个任务的名字与默认工厂绑定。注意第 38 行的 `RIME_REGISTER_CUSTOM_MODULE(levers)` 把这个初始化函数声明为 `levers` 模块的入口，库加载时由模块自注册机制触发（机制见 u5-l3）。

下表把这 11 个注册名与对应的 C++ 类、一句话职责对照清楚（注册顺序即源码顺序）：

| 注册名 | C++ 类 | 一句话职责 |
| --- | --- | --- |
| `detect_modifications` | `DetectModifications` | 比较 `.yaml` 的 mtime 与上次构建时间，判断是否需要重建 |
| `installation_update` | `InstallationUpdate` | 初始化/更新 `installation.yaml`（生成安装 UUID、记录分发版本） |
| `workspace_update` | `WorkspaceUpdate` | 编排一次完整工作区部署（内部调用多个子任务） |
| `schema_update` | `SchemaUpdate` | 更新单个方案并编译其词典 |
| `config_file_update` | `ConfigFileUpdate` | 按 checksum/timestamp 决定是否重新编译单个配置文件 |
| `prebuild_all_schemas` | `PrebuildAllSchemas` | 遍历共享目录，预编译所有 `*.schema.yaml`（安装期用） |
| `user_dict_upgrade` | `UserDictUpgrade` | 升级旧版用户词典格式 |
| `cleanup_trash` | `CleanupTrash` | 把日志与废弃 `.bin` 移入 `trash/` 目录 |
| `user_dict_sync` | `UserDictSync` | 导出/同步所有用户词典 |
| `backup_config_files` | `BackupConfigFiles` | 备份用户配置到同步目录 |
| `clean_old_log_files` | `CleanOldLogFiles` | 清理过期日志文件 |

注意：`deployment_tasks.h` 里声明的 `SymlinkingPrebuiltDictionaries` 类**没有**出现在注册表里——它只被 `WorkspaceUpdate` 在代码里直接 `new` 调用，不对外按名暴露。这是一个值得留意的细节：并非所有任务都走组件注册，编排型任务可以内部直接实例化子任务。

#### 4.1.4 代码实践

实践目标：建立「注册名 ↔ 类」的精确映射，并验证某个任务确实能被按名取到。

操作步骤：

1. 打开 [levers_module.cc:20-30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/levers_module.cc#L20-L30)，数出注册的任务数（应为 11 个）。
2. 对照 [deployment_tasks.h:16-115](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.h#L16-L115) 的类声明，确认 `SymlinkingPrebuiltDictionaries`（第 80 行）确实没有对应的 `r.Register` 行。
3. 在 [deployer.cc:28-40](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L28-L40) 的 `RunTask` 实现里，确认它对未知任务名会打 `ERROR` 并返回 `false`。

需要观察的现象：若调用一个未注册的名字（例如 `deployer.RunTask("nonexistent_task")`），日志里会出现 `unknown deployment task: nonexistent_task`。

预期结果：注册表里恰好 11 项；`SymlinkingPrebuiltDictionaries` 不在其中；未知名调用返回 false 并记 ERROR。若你无法在本地构建运行，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WorkspaceUpdate` 既能作为顶层任务被 `ScheduleTask("workspace_update")` 调度，又能在它内部 `new ConfigFileUpdate(...)` 直接调用子任务？

**答案**：因为每个任务都是普通 C++ 类，`WorkspaceUpdate::Run` 内部完全可以 `new` 出另一个任务对象并直接调它的 `Run(deployer)`（同步、同线程）。组件注册只是提供了「按名字实例化」的能力，并不限制你在代码里直接构造。注册名用于**跨边界/按名调度**场景（如 C API、未来扩展），内部编排则直接用对象。

**练习 2**：如果把 `levers` 模块从 `deployer` 模块组里移除（参见 setup.cc），调用 `deployer.RunTask("workspace_update")` 会发生什么？

**答案**：`DeploymentTask::Require("workspace_update")` 在 `Registry` 里查不到（因为 `levers` 没被加载、没注册任何任务），返回空指针，`RunTask` 打 `unknown deployment task: workspace_update` 并返回 false。

### 4.2 守门任务：DetectModifications 与 InstallationUpdate

#### 4.2.1 概念说明

这两个任务都排在部署链路的**最前面**，作用是「判断要不要继续干」与「确保运行环境就绪」。

- **`DetectModifications`（改动检测）**：扫描指定数据目录里所有 `.yaml`（排除 `user.yaml`）的修改时间，与 `user.yaml` 中记录的 `var/last_build_time` 比较。它的特殊之处在于**返回值语义与其它任务相反**：返回 `true` 表示「检测到改动、需要更新」，`false` 表示「无需更新」。这是因为 C API 用它的返回值来决定是否继续后续重活。
- **`InstallationUpdate`（安装信息更新）**：负责创建/更新用户目录下的 `installation.yaml`，里面记录本机安装的唯一 ID（UUID）、分发版本、RIME 版本与同步目录。这个 UUID 就是用户词典同步时的「用户身份」（u8-l6 中的 `user_id`）。

#### 4.2.2 核心流程

`DetectModifications::Run` 的判定逻辑可写成：

\[
\text{needs\_update} = \left(\max_{\,f \in \text{yaml 文件}} \text{mtime}(f)\right) \;>\; \text{last\_build\_time}
\]

即「最新一个 yaml 的修改时间」超过了「上次构建时间」就返回 true。

`InstallationUpdate::Run` 的流程：

```
读 installation.yaml（若存在）
  ├─ 取出 installation_id / sync_dir / backup_config_files / 旧分发版本
  └─ 回填到 deployer->user_id / sync_dir / backup_config_files
若 (已有 id) 且 (分发名/版本/RIME_VERSION 全部未变) → 直接返回 true（无需重写）
否则
  ├─ id 为空 → 生成新 UUID，写 install_time
  ├─ id 已有但版本变 → 写 update_time
  └─ 写入 distribution_* / rime_version，SaveToFile
```

注意它把读到的 `sync_dir`、`backup_config_files` 回填进 `deployer`——这是「配置反哺调度器」的地方，后续 `BackupConfigFiles` 会读 `deployer->backup_config_files` 决定是否真的备份。

#### 4.2.3 源码精读

[deployment_tasks.cc:44-80](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L44-L80) — `DetectModifications::Run`：第 47-63 行遍历 `data_dirs_`（由调用方传入，通常是 `user_data_dir` 与 `shared_data_dir`），用 `std::max` 取所有 `.yaml` 文件 mtime 的最大值；第 72-74 行从 `user_config` 读 `var/last_build_time`；第 75 行做比较。注意第 19-21 行头文件里的注释特别强调：它的返回值含义与其它任务不同（true = 需要更新）。

[deployment_tasks.h:16-25](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.h#L16-L25) — `DetectModifications` 类声明与「返回值语义相反」的注释。

[deployment_tasks.cc:82-164](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L82-L164) — `InstallationUpdate::Run`：第 86-92 行确保用户目录存在；第 93-126 行读取既有 `installation.yaml` 并回填 `deployer`；第 127-132 行若身份与版本都没变则提前返回；第 134-163 行生成 UUID、写时间戳与版本信息后 `SaveToFile`。其中第 139 行用 `boost::uuids` 生成安装 ID。

#### 4.2.4 代码实践

实践目标：理解 `DetectModifications` 在 `start_maintenance` 里扮演的「短路」角色。

操作步骤：

1. 读 [rime_api_impl.h:65-89](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L65-L89) 的 `RimeStartMaintenance`。注意 `full_check` 为 false 时，它先 `RunTask("detect_modifications", args)`，**只有返回 true（检测到改动）才继续**排后续任务。
2. 想象两种场景：① 你刚编辑了 `default.yaml`；② 自上次部署后没动任何 yaml。
3. 对照 `DetectModifications::Run` 的返回逻辑，分别说出两种场景下 `start_maintenance(false)` 的行为。

需要观察的现象：场景①中，`detect_modifications` 返回 true，`workspace_update` 等被排进队列，部署真正发生；场景②中，`detect_modifications` 返回 false，`RimeStartMaintenance` 在第 80 行 `return False` 提前结束，**不排任何后续任务**——这是一次「无变更短路」，避免无谓的重建。

预期结果：能复述「mtime 比较 + 短路」机制。若你本地有构建产物，可手动 `touch` 一个 `.yaml` 后调用 `start_maintenance(false)` 验证日志出现 `modifications detected. workspace needs update.`，否则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`DetectModifications` 在 [deployment_tasks.cc:64-67](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L64-L67) 捕获 `filesystem_error` 时返回 `true`，为什么不是 `false`？

**答案**：返回 true 是**保守策略**——读文件信息失败（权限不足、路径不存在等）时，宁可认为「可能有改动、需要重建」，也不冒险跳过部署。跳过（false）可能导致用户改了配置却没生效。这是一次「宁可多做也不漏做」的取舍。

**练习 2**：`InstallationUpdate` 读取到的 `sync_dir` 是怎么影响后续任务的？

**答案**：第 108 行把它回填进 `deployer->sync_dir`，而 `Deployer::user_data_sync_dir()` 返回 `sync_dir / user_id`（[deployer.cc:150-152](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/deployer.cc#L150-L152)），这正是 `BackupConfigFiles`（4.4 节）和用户词典同步落盘的目录。所以 `InstallationUpdate` 必须先于备份/同步任务运行。

### 4.3 核心编译链：ConfigFileUpdate、SchemaUpdate 与 DictCompiler、WorkspaceUpdate

#### 4.3.1 概念说明

这是部署的**真正重活**所在，三个任务层层嵌套、互为依赖：

- **`ConfigFileUpdate`（单配置文件更新）**：对**任意一个**配置文件（如 `default.yaml`、`luna_pinyin.schema.yaml`），检查它的 `__build_info/timestamps` 与源文件 mtime 是否一致，不一致就用 `config_builder` 组件重新编译（应用 `__include`/`__patch` DSL 并写新的 `__build_info`）。它是**增量重建的判定单元**。
- **`SchemaUpdate`（单方案更新）**：对一个方案，先 `ConfigFileUpdate` 编译其 `.schema.yaml`，再读 `translator/dictionary` 找到词典名，交给 `DictCompiler` 把 `*.dict.yaml` 编译成 `.bin`。它是**配置编译 → 词典编译**的衔接点。
- **`WorkspaceUpdate`（工作区更新）**：整条链路的**编排者**。它先编译 `default.yaml`、清理旧符号链接，再读 `default.yaml` 里的 `schema_list`，对每个方案（及其依赖方案）跑一遍 `SchemaUpdate`，最后把当前时间写回 `var/last_build_time`。

依赖关系是本节核心：

```
WorkspaceUpdate
   ├─ ConfigFileUpdate("default.yaml")        ← 编译全局配置
   ├─ SymlinkingPrebuiltDictionaries           ← 清理旧符号链接
   └─ 对 schema_list 中每个方案:
         SchemaUpdate(schema_path)
            ├─ ConfigFileUpdate("<id>.schema.yaml")   ← 编译方案配置
            └─ 若有 translator/dictionary:
                  DictCompiler::Compile()              ← 编译词典 → .bin
```

即「`SchemaUpdate` 依赖 `ConfigFileUpdate` 产出的编译后配置，再依赖 `DictCompiler` 产出的二进制词典」——这正是 practice_task 要你梳理的依赖链。

#### 4.3.2 核心流程

**`ConfigFileUpdate::Run` 判定逻辑**（核心是 `ConfigNeedsUpdate`）：

```
读 __build_info/timestamps（一张 {源文件名: mtime} 表）
对每条记录:
   重新解析源文件路径，取其当前 mtime
   若 recorded_time != 当前 mtime → 需要更新（源文件改了/被删了）
若任一不一致或缺少 build_info → 用 config_builder 重新编译并写 staging
```

**`SchemaUpdate::Run` 流程**：

```
1. LoadFromFile(source_path_)，取 schema/schema_id
2. ConfigFileUpdate("<id>.schema.yaml", "schema/version")  ← 依赖①
3. 用 "schema" 组件重新加载编译后的配置
4. 读 translator/dictionary；若没有 → 直接返回 true（该方案不需要词典）
5. 构造 Dictionary（Require("dictionary")->Create({&schema, "translator"})）
6. DictCompiler(dict).Compile(compiled_schema)             ← 依赖②：产出 .bin
```

第 4 步是关键分支：**并非所有方案都有词典**（例如纯标点方案），没有 `translator/dictionary` 时 `SchemaUpdate` 在配置编译完就结束，不触发 `DictCompiler`。

**`WorkspaceUpdate::Run` 流程**：先跑两个前置子任务，再遍历 `schema_list`。对每个方案，除了构建它本身，还会读它的 `schema/dependencies` 列表，把依赖方案也一并构建（标记 `as_dependency=true`，缺失只警告不报错）。最后写 `last_build_time`。

#### 4.3.3 源码精读

[deployment_tasks.cc:431-452](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L431-L452) — `ConfigFileUpdate::Run`：第 438-442 行先把旧版本产生的废弃用户副本移入 `trash`；第 444 行用 `config` 组件加载；第 445 行 `ConfigNeedsUpdate` 判定；第 449 行**关键**——需要更新时 `config.reset(Config::Require("config_builder")->Create(file_name_))`，从「直接读 YAML」的 `config` 组件切换到「带编译器」的 `config_builder` 组件。

[deployment_tasks.cc:393-429](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L393-L429) — `ConfigNeedsUpdate`：遍历 `__build_info/timestamps`，逐个比对源文件当前 mtime 与记录值。第 395-398 行说明「连 build_info 都没有」直接判为需要更新（首次编译）。

[deployment_tasks.cc:328-381](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L328-L381) — `SchemaUpdate::Run`：第 342-346 行先 `ConfigFileUpdate`（依赖①）；第 348 行重新加载编译后配置；第 350-353 行读 `translator/dictionary`，没有就提前返回；第 355-360 行构造 `Dictionary`；第 367-378 行**依赖②**——`DictCompiler dict_compiler(dict.get())` 后调 `Compile(compiled_schema)`，失败打 ERROR 返回 false。第 368-370 行 `verbose_` 模式会带 `kRebuild | kDump` 选项强制全量重建并转储。

[deployment_tasks.cc:166-255](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L166-L255) — `WorkspaceUpdate::Run`：第 170-176 行同步跑 `ConfigFileUpdate("default.yaml")` 与 `SymlinkingPrebuiltDictionaries`；第 184 行读 `schema_list`；第 196-222 行定义 `build_schema` lambda，它对单个方案 `new SchemaUpdate(schema_path)` 并 `Run`；第 236-245 行处理 `schema/dependencies`，递归构建依赖方案（`as_dependency=true`）；第 252 行写回 `var/last_build_time`（这正是 4.2 节 `DetectModifications` 下次比较的基准）。

[deployment_tasks.cc:257-263](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L257-L263) — `SchemaUpdate` 从 `TaskInitializer`（`std::any`）里 `any_cast<path>` 取出源路径，这是「按名调度时传 path 参数」的入口（对应 C API `deploy_schema`）。

#### 4.3.4 代码实践

实践目标：追踪一次 `RimeDeployWorkspace()` 的同步执行顺序与产物依赖。

操作步骤：

1. 读 [rime_api_impl.h:118-124](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L118-L124) 的 `RimeDeployWorkspace`，它**同步**依次 `RunTask` 四个任务：`installation_update` → `workspace_update` → `user_dict_upgrade` → `cleanup_trash`。
2. 展开 `workspace_update` 内部（见 4.3.1 的依赖图），写出完整的扁平执行序列。
3. 标注每一步的**产物**与**下一步对它的依赖**。

需要观察的现象（应得到的依赖表）：

| 步骤 | 任务 | 产物 | 下一步如何依赖它 |
| --- | --- | --- | --- |
| 1 | `installation_update` | `installation.yaml` + 回填 `deployer` | 后续备份任务读 `deployer->sync_dir` |
| 2 | `ConfigFileUpdate("default.yaml")` | 编译后的 `default.yaml`（含 `__build_info`） | `WorkspaceUpdate` 第 184 行读其中的 `schema_list` |
| 3 | `SymlinkingPrebuiltDictionaries` | 清理掉指向共享数据的旧符号链接 | 为后续用户目录写入腾位 |
| 4 | 对每个 schema: `ConfigFileUpdate("<id>.schema.yaml")` | 编译后的方案配置 | `SchemaUpdate` 第 348 行重新加载它，读 `translator/dictionary` |
| 5 | 对每个 schema: `DictCompiler::Compile` | `.table.bin`/`.prism.bin`/`.reverse.bin` | 运行期 `Dictionary` 用 mmap 挂载它们（u8-l1/u8-l5） |
| 6 | （回到顶层）`user_dict_upgrade` | 升级后的用户词典 | 运行期 `UserDictionary` 读取 |
| 7 | `cleanup_trash` | 日志/废弃 bin 移入 `trash/` | 无下游依赖，纯清理 |

预期结果：能清晰说出「`SchemaUpdate` 依赖 `ConfigFileUpdate`（要编译后的配置才能读 `translator/dictionary`）与 `DictCompiler`（要 `.bin` 才能在运行期查词典）」这条核心链。这也是 practice_task 的答案雏形。若你要本地验证，可在 staging 目录观察产物文件的生成顺序，否则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SchemaUpdate` 在 `ConfigFileUpdate` 之后要**重新加载**配置（第 348 行 `config.reset(Config::Require("schema")->Create(schema_id))`），而不是直接用第 336 行 `LoadFromFile` 得到的 `config`？

**答案**：第 336 行的 `LoadFromFile` 读的是**原始 YAML**，没有应用 `__include`/`__patch` DSL；而 `ConfigFileUpdate` 用 `config_builder` 重新编译后会产出**合并后的**配置（含从 `default.yaml` 注入的 `menu`/`navigator` 等，见 u4-l4）。`translator/dictionary` 这种键可能在编译/合并后才完整，所以必须读编译后的版本。

**练习 2**：`ConfigFileUpdate::Run` 末尾第 451 行无论是否真的重新编译都 `return true`。这是否意味着它永远「成功」？

**答案**：它的 `return true` 表示「流程走通」（含「无需更新」这种正常情况），而真正表示「需要更新并重编译」的副作用发生在第 445-450 行——只有 `ConfigNeedsUpdate` 为真时才会切到 `config_builder` 重编译。返回值不区分「重建了」还是「跳过了」，调用方（`SchemaUpdate`）也不关心，因为后续读编译后配置时拿到的是最新结果。真正的失败（如目录创建失败）会在第 446-448 行提前 `return false`。

**练习 3**：`WorkspaceUpdate` 处理 `schema/dependencies` 时，为什么对依赖方案用 `as_dependency=true`？

**答案**：依赖方案缺失时，`as_dependency=true` 让 `build_schema` 只打 `WARNING` 并跳过（[deployment_tasks.cc:208-210](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L208-L210)），不算失败、不增加 `failure` 计数；而主方案缺失会打 `ERROR` 并计入失败。这是「缺依赖可容忍，缺主方案不行」的容错分级。

### 4.4 安装期与辅助任务族

#### 4.4.1 概念说明

剩下的一批任务要么是**安装期一次性**的，要么是**维护性清理/同步**，彼此相对独立，没有 4.3 那种层层嵌套的依赖。可分为三组：

- **安装期预构建**：`PrebuildAllSchemas`（预编译共享目录下所有方案，供发行版打包用）与 `SymlinkingPrebuiltDictionaries`（在用户目录建立指向预构建产物的符号链接，避免每个用户都重新编译）。
- **用户词典维护**：`UserDictUpgrade`（把旧版用户词典格式升级到当前版本）与 `UserDictSync`（把所有用户词典导出为可同步的文本快照，见 u8-l6）。
- **配置备份与垃圾清理**：`BackupConfigFiles`（把用户配置备份到同步目录）、`CleanupTrash`（把日志和废弃 `.bin` 移入 `trash/`）、`CleanOldLogFiles`（清理 glog 产生的过期日志）。

`PrebuildAllSchemas` 的定位值得强调：它面向**发行版打包者**而非普通用户——在打包机上对共享数据目录里所有方案跑一遍 `SchemaUpdate`，把产物随发行版一起分发，终端用户就不必在首次启动时漫长等待。

#### 4.4.2 核心流程

**`PrebuildAllSchemas::Run`**：

```
遍历 shared_data_dir
对每个 *.schema.yaml:
   new SchemaUpdate(entry)->Run(deployer)   ← 复用 4.3 的单方案链路
任一失败 → success=false
```

**`UserDictUpgrade::Run`**：

```
LoadModules(kLegacyModules)                 ← 加载 legacy 模块（含 legacy_userdb 后端）
Require("legacy_userdb")
若不存在 → return true（无旧库可升级）
UserDictManager 列出所有旧格式用户词典
逐个 UpgradeUserDict
```

**`UserDictSync::Run`**：直接 `UserDictManager(deployer).SynchronizeAll()`，内部对每个用户词典导出 `.userdb.txt` 文本快照（u8-l6）。

**`BackupConfigFiles::Run`**：

```
若 deployer->backup_config_files 为 false → 跳过
遍历 user_data_dir 下 *.yaml / *.txt
   若已备份且 checksum 一致 → 跳过（latest）
   若是用户定制副本（含 customization 字段）→ 跳过（skipped）
   否则 copy_file 到 user_data_sync_dir()
```

**`CleanupTrash::Run`**：把 `rime.log`、`*.bin`、`*.reverse.kct`、`*.userdb.kct.old`、`*.userdb.kct.snapshot` 等移入 `trash/`。

**`CleanOldLogFiles::Run`**：仅 `RIME_ENABLE_LOGGING` 下编译；扫描 glog 目录，删除非当天的 `*.log`，但保留正在使用的（通过符号链接判断）。

#### 4.4.3 源码精读

[deployment_tasks.cc:454-470](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L454-L470) — `PrebuildAllSchemas::Run`：第 460-468 行遍历共享目录，对每个 `.schema.yaml` 直接 `new SchemaUpdate(entry)` 跑一遍。注意它对应 C API `RimePrebuildAllSchemas`（[rime_api_impl.h:113-116](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L113-L116)），是同步 `RunTask`。

[deployment_tasks.cc:472-503](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L472-L503) — `SymlinkingPrebuiltDictionaries::Run`：注意这个任务当前的实现只做**清理**（第 481-501 行删除指向共享数据的旧符号链接和悬挂链接），不再创建新链接——名字里的 "Symlinking" 保留了历史含义。它被 `WorkspaceUpdate` 第 175 行直接 `new` 调用，所以无需注册进 `Registry`。

[deployment_tasks.cc:505-520](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L505-L520) — `UserDictUpgrade::Run`：第 506 行 `LoadModules(kLegacyModules)` 按需加载 legacy 模块（[setup.cc:43](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/setup.cc#L43)），第 507 行 `Require("legacy_userdb")`，第 508-510 行若组件不存在直接返回 true（没装 legacy 模块就无旧库可升）。

[deployment_tasks.cc:522-525](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L522-L525) — `UserDictSync::Run`：一行委托给 `UserDictManager::SynchronizeAll()`。

[deployment_tasks.cc:541-586](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L541-L586) — `BackupConfigFiles::Run`：第 542-545 行受 `deployer->backup_config_files` 开关控制（该开关由 `InstallationUpdate` 回填，见 4.2）；第 565-568 行用 `Checksum` 比对避免重复备份；第 569-572 行跳过用户定制副本（含 `customization` 字段的文件，见第 527-539 行 `IsCustomizedCopy`）。

[deployment_tasks.cc:588-622](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L588-L622) — `CleanupTrash::Run`：第 600-603 行列出要清理的扩展名，第 609 行 `fs::rename` 移入 `trash/`。

[deployment_tasks.cc:624-695](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L624-L695) — `CleanOldLogFiles::Run`：整个实现被 `#ifdef RIME_ENABLE_LOGGING` 包裹（第 626、693 行），关闭日志时是空函数。它被 `RimeStartMaintenance` 第 68 行**第一个**调用（[rime_api_impl.h:68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L68)）。

#### 4.4.4 代码实践

实践目标：对比「工作区部署」与「用户数据同步」两条不同链路用了哪些辅助任务。

操作步骤：

1. 读 [rime_api_impl.h:138-145](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L138-L145) 的 `RimeSyncUserData`，看它 `ScheduleTask` 了哪三个任务（`installation_update` / `backup_config_files` / `user_dict_sync`），然后 `StartMaintenance()`。
2. 读 [rime_api_impl.h:84-87](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L84-L87) 的 `RimeStartMaintenance` 末尾，看工作区部署链路 `ScheduleTask` 了哪三个任务（`workspace_update` / `user_dict_upgrade` / `cleanup_trash`）。
3. 列表对比两条链路的任务集合，找出「只在同步链路出现」与「只在部署链路出现」的任务。

需要观察的现象：

| 任务 | 部署链路 (`start_maintenance`) | 同步链路 (`sync_user_data`) |
| --- | :---: | :---: |
| `clean_old_log_files`（同步前置） | ✓ | ✗ |
| `installation_update` | ✓（同步前置） | ✓（异步排队） |
| `workspace_update` | ✓ | ✗ |
| `user_dict_upgrade` | ✓ | ✗ |
| `cleanup_trash` | ✓ | ✗ |
| `backup_config_files` | ✗ | ✓ |
| `user_dict_sync` | ✗ | ✓ |

预期结果：同步链路**不重建方案/词典**（没有 `workspace_update`），只做「备份配置 + 导出用户词典」这类**可逆的导出操作**；部署链路则做**重构建**。理解这条区分对排查「为什么同步后没生效，要 deploy 才生效」很关键。若无法本地运行，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`SymlinkingPrebuiltDictionaries` 名字里有 "Symlinking"（建立符号链接），但当前实现里并没有创建符号链接的代码。结合它的调用位置，解释这个现象。

**答案**：这是历史遗留——早期版本会在用户目录建立指向共享目录预构建词典的符号链接以节省用户端编译，名字由此而来。当前实现（[deployment_tasks.cc:480-501](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L480-L501)）退化为只做**清理旧链接**（删除指向共享数据的链接与悬挂链接），创建逻辑已移除，但类名保留。它被 `WorkspaceUpdate` 第 175 行直接 `new` 调用，所以即便没注册进 `Registry` 也能用。

**练习 2**：`UserDictUpgrade` 为什么要先 `LoadModules(kLegacyModules)` 再 `Require("legacy_userdb")`？

**答案**：`legacy_userdb` 后端（读旧版用户词典格式的组件）注册在 `legacy` 模块里，而 `legacy` 模块默认不在 `deployer` 模块组（core+dict+levers）中，不会随部署自动加载。所以必须显式 `LoadModules(kLegacyModules)` 触发它的注册，`Require` 才能查到。查不到时（没编入 legacy 模块），第 508-510 行直接返回 true，表示「没有可升级的旧库」。

**练习 3**：`CleanOldLogFiles` 为什么用 `#ifdef RIME_ENABLE_LOGGING` 包裹整个实现？

**答案**：它依赖 glog 的 `FLAGS_logtostderr`/`FLAGS_log_dir`/`google::GetLoggingDirectories()` 等符号（[deployment_tasks.cc:627-637](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/deployment_tasks.cc#L627-L637)）。当 librime 以 `ENABLE_LOGGING=OFF` 构建时（u1-l2），glog 不被链接，这些符号不存在，必须用宏屏蔽整个函数体，只保留 `return success;`，否则链接报错。

## 5. 综合实践

**任务**：画出一次「用户改了 `luna_pinyin.schema.yaml` 的 speller 段后，调用 `start_maintenance(false)`」的完整任务执行时序图，并标注每一步的同步/异步属性、产物与下游依赖。

要求：

1. 从 [rime_api_impl.h:65-89](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L65-L89) 的 `RimeStartMaintenance` 出发，**同步阶段**依次列出 `clean_old_log_files`、`installation_update`、`detect_modifications`（带 `user_data_dir` 与 `shared_data_dir` 两个参数）。
2. 说明为什么 `detect_modifications` 这次会返回 true（因为你刚改了 yaml，其 mtime > `var/last_build_time`）。
3. **异步阶段**（`StartMaintenance` 后在工作线程跑）列出排队的 `workspace_update`、`user_dict_upgrade`、`cleanup_trash`。
4. 展开 `workspace_update` 内部：`ConfigFileUpdate("default.yaml")` → `SymlinkingPrebuiltDictionaries` → 遍历 `schema_list` → 对 `luna_pinyin` 跑 `SchemaUpdate` → 内部 `ConfigFileUpdate("luna_pinyin.schema.yaml")` → 因 speller 变化触发 **Prism 重建**（`DictCompiler` 检测到拼写代数 checksum 变化，只重建 `.prism.bin` 而非整个 table，参见 u8-l1/u8-l4）→ 写回 `var/last_build_time`。
5. 用箭头标出依赖：`ConfigFileUpdate` 产物（编译后配置）→ 被 `SchemaUpdate` 读 `translator/dictionary` 依赖；`DictCompiler` 产物（`.prism.bin`）→ 被运行期 `Prism` mmap 依赖。

完成后再回答一个思考题：如果用户改的是 `luna_pinyin.dict.yaml`（词典源文件，加了新词条）而不是 schema，上述链路里哪一步的重建范围会不同？

> 参考答案：改 `.dict.yaml` 时，`SchemaUpdate` 内 `DictCompiler` 的 checksum 检测到**词典**变化（而非拼写代数），会重建 `.table.bin` + `.prism.bin` + `.reverse.bin` 全部三类产物（u8-l4），而改 schema 的 speller 段只重建 `.prism.bin`。这条链路的其余部分（配置编译、方案遍历）不变。

## 6. 本讲小结

- `levers` 模块在初始化期把 **11 个部署任务**用 `new Component<XxxTask>` 注册进 `Registry`，按名 `Require` 实例化；`deployer` 模块组 = core + dict + levers，确保部署期加载。
- **`DetectModifications`** 返回值语义特殊（true = 需要更新），是 `start_maintenance(false)` 的短路开关；**`InstallationUpdate`** 生成安装 UUID 并把 `sync_dir`/`backup_config_files` 回填进 `deployer`。
- 核心编译链层层嵌套：**`WorkspaceUpdate`** 编排 → 对每个方案跑 **`SchemaUpdate`** → 内部先 **`ConfigFileUpdate`** 编译配置、再 **`DictCompiler::Compile`** 编译词典；`SchemaUpdate` 依赖编译后配置（读 `translator/dictionary`）与 `.bin` 产物。
- `ConfigFileUpdate` 用 `__build_info/timestamps` 与源文件 mtime 比对做**增量重建**，需要更新时从 `config` 组件切到 `config_builder` 组件。
- **`PrebuildAllSchemas`** 面向发行版打包（预编译所有方案），**`SymlinkingPrebuiltDictionaries`** 当前退化为清理旧链接，二者属安装期任务。
- 同步链路（`sync_user_data`）与部署链路（`start_maintenance`）任务集合不同：前者只备份配置 + 导出用户词典（可逆），后者重建方案与词典（重活）。

## 7. 下一步学习建议

- **u9-l3 Customizer 与用户设置**：本讲的 `ConfigFileUpdate` 用 `config_builder` 编译配置时会应用 `__include`/`__patch`，而用户的 `*.custom.yaml` 正是通过 `__patch` 注入的——下一篇讲 `Customizer` 如何把这些用户定制合并进官方配置。
- **u9-l4 Switcher 与 Switches**：`WorkspaceUpdate` 读了 `default.yaml` 的 `schema_list`，而方案的切换交互由 `Switcher` 完成，建议接着读。
- **重读 u8-l4 DictCompiler 构建流程**：本讲多次提到 `SchemaUpdate` 依赖 `DictCompiler`，建议回到 u8-l4 对照 checksum 驱动的增量重建细节（改词条 vs 改拼写代数的不同重建范围）。
- **源码延伸**：若对用户词典的同步/恢复感兴趣，可读 [src/rime/lever/user_dict_manager.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/lever/user_dict_manager.cc) 与 u8-l6 提到的 `user_db_recovery_task`，它们与本讲的 `UserDictSync`/`UserDictUpgrade` 共同构成用户词典的维护闭环。
