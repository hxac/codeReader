# msprof 用户视角：msprof 命令、环境变量与 acl.json 采集方式

## 1. 本讲目标

前几讲（u4-l1 ~ u4-l4）我们站在 msprof **实现者**视角读了 collector 的架构、入口、插件和分析器。本讲切换到**使用者**视角，学完后你应该能够：

1. 说出 msprof 的三类典型采集触发方式（命令行、acl.json/ACL API、环境变量），以及各自适用的场景。
2. 掌握 `PROFILING_MODE` 与 `PROFILING_OPTIONS` 两个环境变量的取值含义、配置格式和优先级约束。
3. 区分「延迟采集」（--delay/--duration）与「动态采集」（--dynamic=on 交互模式）这两种时间维度上的采集控制手段。
4. 能把用户文档里的每个命令行参数，对应到 u4-l2 读过的 C++ 入口源码（`LONG_OPTIONS` 表 / `ProfileParams`）上，做到「文档 ↔ 源码」互查。

## 2. 前置知识

- **Profiling（性能分析）**：在 AI 任务运行时记录算子耗时、内存带宽、通信带宽等数据，用于定位性能瓶颈。msprof 采集的原始数据落盘在 `PROF_XXX` 目录中，再由分析侧（Python wheel）解析成可视化结果。
- **ACL（Ascend Computing Language）**：昇腾的异构计算编程接口，离线推理的 C&C++ 程序通常基于它开发。`aclInit()` 是所有 ACL 程序的第一个调用。
- **在线推理 / 离线推理**：在线推理指框架（如 TensorFlow）常驻服务里跑模型；离线推理指用户自己写的 C&C++ 程序直接调 ACL 接口执行模型。
- **launch 与 attach**：两种"采集器如何找到业务进程"的方式。launch 是 msprof 亲手拉起业务进程；attach 是业务进程先跑起来，msprof 再通过 PID 挂上去。
- **交互模式**：动态采集时 msprof 提供的一个 `(msprof)` 提示符，可以在里面敲 `start`/`stop`/`quit` 命令随时起停采集。
- 建议先回顾 u4-l1（msprof 总体架构：C++ collector 与 Python 分析 wheel 的分工）和 u4-l2（msprofbin 入口与 `ProfileParams` 参数收敛），本讲会多次回指它们。

## 3. 本讲源码地图

本讲的"源码"以**用户文档**为主、C++ 入口代码为辅（文档本身就是 msprof 对外契约的一部分，和代码同等重要）：

| 文件 | 作用 |
| --- | --- |
| `docs/zh/profiling/msprof_cmd/msprof_cmd.md` | msprof 采集命令总目录页，列出 7 个子主题 |
| `docs/zh/profiling/msprof_cmd/general_collect_commands.md` | msprof 通用命令：`msprof [options] <app>` 格式、app/options 参数说明 |
| `docs/zh/profiling/msprof_cmd/delayed_mode.md` | 延迟采集：`--delay`/`--duration` 参数 |
| `docs/zh/profiling/msprof_cmd/dynamically.md` | 动态采集：launch/attach 两种方式与交互命令 |
| `docs/zh/profiling/other_method/with_environment_variables.md` | 环境变量采集方式的操作步骤 |
| `docs/zh/env-vars/PROFILING_MODE.md` | `PROFILING_MODE` 环境变量定义（true/false/dynamic） |
| `docs/zh/env-vars/PROFILING_OPTIONS.md` | `PROFILING_OPTIONS` 全量采集项字典说明 |
| `docs/zh/profiling/other_method/with_acljson_config_file.md` | acl.json 配置文件采集方式 |
| `docs/zh/profiling/other_method/with_acl_apis.md` | ACL Profiling API 采集方式 |
| `src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp` | C++ 命令行入口 `main()`（u4-l2 已精读） |
| `src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp` | 命令行参数表与校验，本讲用它佐证文档参数真实存在 |
| `src/msprof/collector/dvvp/app/application.cpp` | 拉起业务进程时注入环境变量的代码 |
| `src/msprof/collector/dvvp/common/config/config.h` | 环境变量名字符串常量定义 |

## 4. 核心概念与源码讲解

### 4.1 msprof 命令行采集方式：一切采集的基础入口

#### 4.1.1 概念说明

命令行方式是 msprof 最主流的采集入口：用户敲一条 `msprof [options] <app>`，msprof 以 launch 方式拉起业务程序，在程序运行期间采集性能数据，程序退出后自动停止采集并解析落盘。u4-l1 讲过，用户敲的 `msprof` 命令就是 msprofbin 构建目标改名安装到 `tools/profiler/bin` 的产物——所以「命令行方式」在源码侧对应的就是 u4-l2 精读的 `msprof_bin.cpp` 主流程。

文档把命令行方式组织成 7 个子主题（通用命令、AI 任务运行数据、AI 处理器系统数据、Host 侧系统数据、msproftx 数据、动态采集、延迟采集），后两个是时间维度的变体，放在 4.4 节单独讲。

#### 4.1.2 核心流程

命令行采集的完整生命周期：

```text
用户敲 msprof [options] <app>
    │
    ├─ input_parser.cpp 解析 options（表驱动，见 u4-l2）→ 收敛进 ProfileParams
    ├─ MsprofManager 按参数生成 RunningMode（app 模式优先级最高）
    ├─ application.cpp 组装子进程环境变量并 fork/exec 拉起 <app>
    ├─ 采集器随业务运行持续落盘原始数据
    ├─ 业务进程退出（或 Ctrl+C 触发 SIGINT 链路，见 u4-l2 的 Stop 链路）
    └─ 自动解析导出，--output 目录下生成 PROF_XXX/ 及解析结果
```

两种传入业务程序的方式：

- 方式一（推荐）：命令末尾直接跟程序或脚本，如 `msprof --output=... /home/projects/main arg1 arg2`；
- 方式二：`--application="<app> <args>"` 整体作为一个字符串传入。

#### 4.1.3 源码精读

命令格式与两种 app 传参方式定义在通用命令文档中：

- [general_collect_commands.md:L14-L26](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/general_collect_commands.md#L14-L26)：定义「方式一（推荐）末尾直接传入用户程序」和「方式二 --application 参数传入」两种命令格式。
- [general_collect_commands.md:L63-L67](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/general_collect_commands.md#L63-L67)：说明 app 参数的必选性随采集类型变化——采集 AI 任务运行数据时必选，采集系统数据时可选。这正对应 u4-l2 读过的 MsprofManager 模式生成优先级（app > devices > host_sys > ...）。
- [general_collect_commands.md:L71-L103](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/general_collect_commands.md#L71-L103)：通用 options（`--output`、`--type`、`--environment`、`--storage-limit`、`--help`）的说明，其中 `--storage-limit` 的磁盘老化删除策略与 `PROFILING_OPTIONS` 里的 `storage_limit` 字段语义一致（详见 4.2.3）。

文档里的命令不是凭空承诺——每个选项在 C++ 入口的参数表中都有唯一定义点：

- [msprof_bin.cpp:L89](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp#L89)：`int main(int argc, const char **argv, const char **envp)`，msprof 命令的 C++ 入口（u4-l2 已精读其固定初始化顺序）。
- [input_parser.cpp:L2439-L2455](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp#L2439-L2455)：参数表中声明了 `dynamic`、`delay`、`duration` 三个 Args 结构（名字 + 帮助文案 + 默认值），这就是动态/延迟采集参数在源码中的唯一定义点，帮助文案中的取值范围（1 ~ 4294967295s）与文档表格完全一致。

#### 4.1.4 代码实践

**实践目标**：验证「文档参数 ↔ 源码参数表」一一对应。

**操作步骤**：

1. 打开 [input_parser.cpp:L2439](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp#L2439) 附近的 `argsList_` 构建代码。
2. 通读该函数，把所有 `Args xxx = {...}` 的第一字段（参数名字符串）抄成一个清单。
3. 对照 [general_collect_commands.md:L69-L103](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/general_collect_commands.md#L69-L103) 及其链接的子页面，标注每个参数出现在哪份文档里。

**需要观察的现象**：参数表中的每个名字都能在文档中找到；反之文档中的通用参数（如 `--output`、`--type`）也能在表中找到。

**预期结果**：得到一张三列对照表（参数名 / 源码帮助文案 / 文档说明位置）。若有个别参数在两边对不上，记下来——那可能是仅在特定 RunningMode 下才启用的参数，正好可以回看 u4-l2 的 ProcessOptions 按枚举区间路由的逻辑。

#### 4.1.5 小练习与答案

**练习 1**：为什么文档推荐方式一（命令末尾直接传 app）而不是 `--application`？

**答案**：[general_collect_commands.md:L63-L65](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/general_collect_commands.md#L63-L65) 说明：若 parameter 中存在异常符号将无法识别参数；且参数值需要加引号时，建议把命令写入 shell 脚本再由 msprof 执行脚本。方式一把 app 与其参数作为独立的 argv 传给 msprofbin，避免了字符串二次解析的歧义。

**练习 2**：`--output` 不配置时，数据落在哪里？

**答案**：[general_collect_commands.md:L81-L83](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/general_collect_commands.md#L81-L83)：方式一（末尾直接传 app）默认落盘在**当前目录**；方式二（--application）默认落盘在 **AI 任务文件所在目录**。两种方式的默认值不同，排障时找不到数据目录先确认自己用的是哪种方式。

### 4.2 环境变量方式：PROFILING_MODE 与 PROFILING_OPTIONS

#### 4.2.1 概念说明

命令行方式要求 msprof 亲手拉起业务进程（launch）。但有些场景业务进程由别的调度系统拉起，msprof 插不进启动命令——这时可以**在业务进程自己的环境里**预置两个环境变量，让进程内的 CANN 软件栈自己开启采集：

- `PROFILING_MODE`：总开关。
- `PROFILING_OPTIONS`：采集项配置，值是一个 **JSON 字符串**。

两个变量配合的语义是：`PROFILING_MODE=true` 时，CANN 软件栈从 `PROFILING_OPTIONS` 读取采集选项；只开 MODE 不配 OPTIONS 也有默认采集集（见 4.2.2）。

#### 4.2.2 核心流程

```text
export PROFILING_MODE=true                      # 1. 开启总开关
export PROFILING_OPTIONS='{"output":..., ...}'  # 2. 声明采集项（JSON 字符串）
启动训练/在线推理任务                             # 3. 进程内 CANN 栈读取环境变量
    → 采集数据落盘到 output 指定目录（PROF_XXX/）
任务结束                                        # 4. 数据解析需另行用 msprof 解析命令
```

`PROFILING_MODE` 的三个取值：

| 取值 | 含义 |
| --- | --- |
| `true` | 开启 Profiling，从 `PROFILING_OPTIONS` 读采集选项 |
| `false` 或不配置 | 关闭 Profiling |
| `dynamic` | 动态采集 attach 方式的**前置配置**：训练任务执行前设置，任务内留出动态采集的挂载点（见 4.4 节） |

`PROFILING_OPTIONS` 的常用字段（完整清单见 4.2.3 引用的文档）：

| 字段 | 作用 | 备注 |
| --- | --- | --- |
| `output` | 采集结果保存路径 | 支持绝对/相对路径，无需提前创建，优先级高于 `ASCEND_WORK_PATH` |
| `storage_limit` | 落盘目录容量上限 | 范围 [200, 4294967295] MB，必须带单位（如 `200MB`），快满时老化删除最早文件 |
| `training_trace` | 迭代轨迹（前向/反向/梯度更新） | 采正向反向算子数据时必须为 on |
| `task_trace` / `task_time` | 算子下发/执行耗时 | 取值 on/off/l0/l1，l0 开销更小，l1 数据更全；task_trace 后续会废弃 |
| `fp_point` / `bp_point` | 迭代轨迹正/反向切点算子名 | 配空串则系统自动识别；动态 shape 场景必须手动配置 |
| `aic_metrics` | AI Core 指标采集项 | 默认 `PipeUtilization`，还支持 Memory、L2Cache、自定义寄存器等 |
| `host_sys` / `host_sys_usage` | Host 侧 CPU/内存/磁盘/网络采集 | 多选用逗号隔开 |

**重要约束**（排障必读）：

1. 这两个环境变量**仅适用于 TensorFlow 训练和在线推理场景**（见 4.2.3 引用的两份「使用约束」），PyTorch 等其他框架请用 msprof 命令行方式包裹。
2. 通过 ACL 接口或 TF Adapter 接口参数 `profiling_mode` 开启 Profiling 的**优先级高于**该环境变量（`PROFILING_MODE=dynamic` 时除外）——即代码里显式开的采集会覆盖环境变量。
3. 使用 `PROFILING_OPTIONS` 前必须先开启 `PROFILING_MODE`。
4. `PROFILING_MODE=true` 但未配 `PROFILING_OPTIONS` 时，默认执行 `training_trace`、`task_trace`、`hccl`、`aicpu` 和 `aic_metrics(PipeUtilization)` 采集，数据保存在当前 AI 任务所在目录。

#### 4.2.3 源码精读

- [with_environment_variables.md:L20-L23](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/other_method/with_environment_variables.md#L20-L23)：环境变量方式的标准配置示例——两行 export，`PROFILING_OPTIONS` 的值是单引号包裹的 JSON 字符串（单引号防止 shell 吞掉双引号）。
- [with_environment_variables.md:L27-L28](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/other_method/with_environment_variables.md#L27-L28)：只开 MODE 不配 OPTIONS 时的默认采集集说明，以及采集结果需另行解析的提示。
- [PROFILING_MODE.md:L3-L20](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/env-vars/PROFILING_MODE.md#L3-L20)：`PROFILING_MODE` 的三个取值定义、配置示例与两条使用约束（仅 TensorFlow 场景；ACL/TF Adapter 接口优先级更高）。
- [PROFILING_OPTIONS.md:L276-L285](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/env-vars/PROFILING_OPTIONS.md#L276-L285)：完整配置示例与使用约束。字段级语义散布在同一文件前文，如 [L7-L14](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/env-vars/PROFILING_OPTIONS.md#L7-L14)（output 路径规则）、[L16-L20](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/env-vars/PROFILING_OPTIONS.md#L16-L20)（storage_limit 老化策略）。

环境变量方式在 C++ 源码侧也有踪迹——msprof 自己拉起业务进程时，会往子进程环境里**写入或改写** `PROFILING_MODE`：

- [config.h:L213](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/common/config/config.h#L213)：`const std::string PROFILING_MODE_ENV = "PROFILING_MODE";`——环境变量名的唯一常量定义点。
- [application.cpp:L201-L211](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/app/application.cpp#L201-L211)：动态采集（launch 方式）时，msprof 组装子进程环境变量列表，**查找并替换**已存在的 `PROFILING_MODE` 项（没有则追加动态采集专用值）。这解释了文档「launch 方式下传入的用户程序中不能设置 PROFILING_MODE 和 PROFILING_OPTIONS」的约束——设了也会被覆盖。
- [application.cpp:L215-L217](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/app/application.cpp#L215-L217)：配置了 `--delay`/`--duration` 时，msprof 主动给子进程追加 `PROFILING_MODE=<延迟采集专用值>` 环境变量——延迟采集正是靠这个开关让 CANN 栈延迟启动采集的。

#### 4.2.4 代码实践

**实践目标**：为一个训练脚本写出环境变量方式的完整配置（纸面实践，可上机则验证）。

**操作步骤**：

1. 写出两行 export 配置（可直接参照 4.5 综合实践的模板，此处先自己写一版）。
2. 若有 TensorFlow + 昇腾环境：在训练脚本启动前 export 这两个变量，跑 1~2 个 step 后停止。
3. 若无环境：只完成配置编写和产物目录推演（见步骤 4）。
4. 画出预期产物目录树：`output` 路径下应出现 `PROF_XXX/`（PROF_ 目录内为原始数据），解析后在其下生成 `mindstudio_profiler_output/`（见 [with_environment_variables.md:L30-L34](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/other_method/with_environment_variables.md#L30-L34)）。

**需要观察的现象**：任务运行期间 output 目录被自动创建（文档明确「该路径无需用户提前创建」）；任务结束后目录中出现 PROF_ 前缀目录。

**预期结果**：环境变量配置无 shell 引号错误（JSON 双引号必须被单引号完整保护）；产物目录结构与推演一致。无环境时标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`PROFILING_OPTIONS` 里 `output` 配了 `/home/user/prof`，同时环境里还有 `ASCEND_WORK_PATH=/data`，数据最终落在哪里？

**答案**：落在 `/home/user/prof`。[PROFILING_OPTIONS.md:L13](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/env-vars/PROFILING_OPTIONS.md#L13) 明确 output 参数优先级高于 `ASCEND_WORK_PATH`。

**练习 2**：用户设了 `PROFILING_MODE=true` 但忘了配 `PROFILING_OPTIONS`，会怎样？

**答案**：不会不采集。[with_environment_variables.md:L27-L28](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/other_method/with_environment_variables.md#L27-L28) 说明此时默认执行 training_trace、task_trace、hccl、aicpu、aic_metrics(PipeUtilization) 五项采集，数据保存在**当前 AI 任务所在目录**（而不是独立 output 目录）。

**练习 3**：`task_trace=l0` 和 `task_trace=l1` 有什么区别？什么时候选 l0？

**答案**：[PROFILING_OPTIONS.md:L26-L35](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/env-vars/PROFILING_OPTIONS.md#L26-L35)：两者都采算子下发/执行耗时，区别是 l1 额外采算子基本信息数据。l0 采集开销更小、耗时统计更精准，适合只关心耗时分布、想降低采集本身对任务的干扰时使用。

### 4.3 acl.json 与 ACL API 方式：离线推理场景的两种代码级入口

#### 4.3.1 概念说明

离线推理（用户自研 C&C++ 程序直接调 ACL）场景下，业务进程不走框架，环境变量方式不适用。msprof 提供两种**写进应用工程**的采集配置方式：

- **acl.json 方式**：把 Profiling 配置写进 acl.json 文件，`aclInit()` 时传入该文件路径，CANN 栈读文件自动开启采集。适合不改动程序逻辑的场景。
- **ACL API 方式**：在代码里显式调用 `aclprofInit` / `aclprofCreateConfig` / `aclprofStart` / `aclprofStop` / `aclprofFinalize` 等接口，精确控制采集的起止区间。适合只想采集某一段推理过程的场景。

两者的关系：acl.json 是声明式的「启动即采集」，ACL API 是命令式的「编程式起停」。

#### 4.3.2 核心流程

acl.json 方式：

```text
推理程序调用 aclInit("../src/acl.json")
    → CANN 栈解析 json 中 profiler 配置（switch/output/...）
    → switch=on 则程序运行期间自动采集
    → 数据落盘到 output 指定目录（默认应用可执行文件所在目录）
    → 生成的 PROF_XXX/ 目录内是原始数据，需拷到装有工具的环境解析
```

ACL API 方式（成对接口是关键纪律）：

```text
aclprofInit(落盘路径)                ←— 与 aclprofFinalize 成对
aclprofCreateConfig(deviceIdList, 指标类型, 类型指针, 采集项掩码) ←— 与 aclprofDestroyConfig 成对
（可选）aclprofSetConfig 扩展配置
aclprofStart(config)                 ←— 与 aclprofStop 成对
    ... 执行模型 / 业务热点区 ...
aclprofStop(config)
aclprofDestroyConfig / aclprofFinalize
```

#### 4.3.3 源码精读

- [with_acljson_config_file.md:L24-L35](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/other_method/with_acljson_config_file.md#L24-L35)：acl.json 方式的接入点——在 `aclInit()` 处传入配置文件路径；若 `aclInit()` 原本传空，需要补上路径并重新编译。
- [with_acljson_config_file.md:L42-L49](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/other_method/with_acljson_config_file.md#L42-L49)：最小可用的 acl.json Profiling 配置——只需 `profiler.switch=on` 与 `profiler.output` 两个键。其余可选键（aic_metrics、task_time、l2、hccl、sys_*_freq 等）与 `PROFILING_OPTIONS` 的字段语义高度同构（见同文档 [L104-L388](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/other_method/with_acljson_config_file.md#L104-L388)）。
- [with_acl_apis.md:L33-L41](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/other_method/with_acl_apis.md#L33-L41)：七个 Profiling API 的职责表，每个接口都标注了与之成对的销毁/停止接口——漏掉成对调用是这类程序最常见的采集失败原因。
- [with_acl_apis.md:L52-L70](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/other_method/with_acl_apis.md#L52-L70)：API 调用示例——`aclprofInit("./output")` 设落盘路径、`aclprofCreateConfig` 传设备列表与 `ACL_PROF_ACL_API | ACL_PROF_TASK_TIME` 采集项掩码、`aclprofSetConfig` 设采样频率。

对比三种方式的参数承载形态，可以清晰看到 u4-l1 讲过的「多入口单收敛为 ProfileParams」设计在用户侧的投影：

| 方式 | 参数载体 | 适用场景 |
| --- | --- | --- |
| msprof 命令行 | argv 选项（input_parser.cpp 的 LONG_OPTIONS 表） | 任意可被 msprof 拉起的任务 |
| 环境变量 | `PROFILING_OPTIONS` JSON 字符串 | TensorFlow 训练/在线推理 |
| acl.json / ACL API | json 文件 / API 参数 | 离线推理（C&C++ ACL 程序） |

三者的采集项名称（task_time、aic_metrics、l2、hccl、storage_limit……）基本同名同义，学会一份即可触类旁通。

#### 4.3.4 代码实践

**实践目标**：为一个假想的 ACL 推理程序补上最小 Profiling 配置（纸面实践）。

**操作步骤**：

1. 写出最小 acl.json 内容（参照 4.3.3 第二条链接的格式，switch=on，output 指向 `./prof_out`）。
2. 找到（或假想）推理工程中 `aclInit()` 的调用处，写出修改后的调用（传入 acl.json 路径）。
3. 推演运行后 `./prof_out` 下的目录结构（应出现 `PROF_数字_时间戳_随机串/` 形式的目录，参见 [with_acljson_config_file.md:L58-L65](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/other_method/with_acljson_config_file.md#L58-L65) 的真实 ls 输出）。
4. 进阶：把同一需求改写成 ACL API 版本，标出三对必须成对的接口。

**需要观察的现象**：应用运行结束后 output 目录出现 PROF_ 前缀目录；异步推理场景进程不退出时数据会持续增长（文档提醒需手动停止进程，预留磁盘）。

**预期结果**：json 格式合法（可先用 `python3 -m json.tool` 校验）；产物目录推演正确。无 ACL 工程环境时标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：acl.json 的 `switch` 配成 `"off"` 或漏配，程序会怎样？

**答案**：[with_acljson_config_file.md:L76-L80](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/other_method/with_acljson_config_file.md#L76-L80)说明 switch 缺失或值不为 on 均表示关闭 Profiling，程序正常运行但不产生性能数据。

**练习 2**：acl.json 里 output 指向的目录运行用户没有写权限，会发生什么？

**答案**：[with_acljson_config_file.md:L91-L96](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/other_method/with_acljson_config_file.md#L91-L96)：默认回退到**应用工程可执行文件所在目录**落盘（前提是该目录有读写权限）。

**练习 3**：为什么 ACL API 方式要强调 `aclprofStart/aclprofStop` 成对使用？

**答案**：Start 下发采集请求、Stop 触发数据收尾落盘。只 Start 不 Stop，进程退出时缓存中的数据可能不完整或不落盘；这与 u4-l2 读过的 MsprofTask「启动-阻塞-唤醒-落盘」节奏、Ctrl+C 时保证 Flush 的 Stop 链路是同一设计要求在 API 层的体现。

### 4.4 延迟采集与动态采集：两种时间维度的采集控制

#### 4.4.1 概念说明

默认情况下 msprof 采集「任务全程」。但全程采集数据量大、且影响性能，实际排障常只需要抓某一段：

- **延迟采集（delayed mode）**：**预先编排**的时间窗口。用 `--delay=N`（延迟 N 秒才开始采）和 `--duration=M`（采 M 秒）在命令行上写死窗口，适合稳定复现的脚本——比如训练要跑 10 分钟才进入稳定期，delay 600 只采稳态段。
- **动态采集（dynamic mode）**：**人工介入**的时间窗口。`--dynamic=on` 进入 `(msprof)` 交互模式，眼睛看着业务指标，觉得该采了敲 `start`，采够了敲 `stop`，适合不知道问题何时出现的不稳定复现场景。

一句话区分：延迟采集是「定时闹钟」，动态采集是「手动快门」。

动态采集又分两种接入方式：

- **launch 方式**：`msprof --dynamic=on ... <app>`，msprof 拉起业务并进入交互模式。适用于 `--application` 拉起的进程就是实际用卡进程的场景。
- **attach 方式**：用户先自行启动 AI 任务（启动前 `export PROFILING_MODE=dynamic`），再 `msprof --dynamic=on --pid=<pid>` 挂载。适用于 run.sh 脚本内部才拉起真正业务进程、或多卡/集群场景（文档推荐多卡用 attach）。

两段式的时序对比：

```text
延迟采集：  app启动 ──────── delay=N ────────▶ [采集 duration=M] ──▶ app结束/窗口结束
动态采集：  app启动 ──▶ msprof进入交互模式 ──▶ 用户敲 start ──▶ [采集] ──▶ 用户敲 stop（可反复多次）
```

#### 4.4.2 核心流程

延迟采集：

```text
msprof --delay=3 --duration=3 /home/projects/MyApp/out/main
    ├─ 参数进 input_parser.cpp → params_->delayTime / params_->durationTime
    ├─ application.cpp 给子进程注入 PROFILING_MODE=<延迟采集值>
    ├─ CANN 栈在 delay 到点后才开始采集
    └─ duration 计时从 delay 结束时刻开始；若 delay 超过任务时长则全程不采
```

动态采集（attach 为例）：

```text
export PROFILING_MODE=dynamic && 启动AI任务     # 任务侧预留挂载点（dynamic_profiling_socket_*）
msprof --dynamic=on --pid=<pid> --output=...    # msprof 挂载
(msprof) start    # 开始采集；start/stop 总次数上限 100（即最多 50 份数据）
(msprof) stop     # 停止；每对 start/stop 在 --output 下生成一个 PROF_*_XXX 目录
(msprof) quit     # 退出交互模式，AI 任务继续正常运行
```

#### 4.4.3 源码精读

延迟采集文档与源码：

- [delayed_mode.md:L21-L30](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/delayed_mode.md#L21-L30)：`--delay`（范围 [1, 4294967295] 秒，默认 0）与 `--duration` 的参数表及使用示例；明确延迟采集与动态采集互斥、仅 AI 任务运行性能数据采集支持。
- [input_parser.cpp:L2202-L2211](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp#L2202-L2211)：命令行解析时把 `dynamic`、`delay`、`duration` 三个选项的值写入 `params_`（即 u4-l2 的 ProfileParams）——文档参数到参数容器的落点。
- [running_mode.cpp:L681-L683](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/running_mode.cpp#L681-L683)：App 模式下的边界防御——业务进程在 delay 时间到之前就退出了（任务时长 ≤ delay），打日志警告「Before delay time, the app process has exited」，即文档所说「配置的时间超过 AI 任务执行时间则不会启动采集」的源码实现。
- [application.cpp:L215-L217](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/app/application.cpp#L215-L217)：配置了 delay/duration 时给子进程追加 `PROFILING_MODE` 环境变量——延迟采集的开关是通过环境变量传递给业务进程内 CANN 栈的。

动态采集文档与源码：

- [dynamically.md:L32-L46](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/dynamically.md#L32-L46)：launch 与 attach 两种命令格式，以及 attach 前置条件 `export PROFILING_MODE=dynamic`。
- [dynamically.md:L53-L60](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/dynamically.md#L53-L60)：交互命令参数表——`start`/`stop` 执行次数总和上限 100（最多 50 份数据）、每对 start/stop 生成一个 PROF_*_*XXX 目录、`quit` 后任务继续运行。
- [dynamically.md:L15-L30](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/dynamically.md#L15-L30)：注意事项清单——launch 方式下用户程序中不能设置 PROFILING_MODE/PROFILING_OPTIONS（会被 application.cpp 覆盖，见 4.2.3）、动态与延迟互斥、同一任务同时只允许一个用户进交互模式、容器场景需在容器内执行 msprof。
- [input_parser.cpp:L1567-L1595](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp#L1567-L1595)：`--dynamic` 与 `--pid`/app 的组合合法性校验——`--dynamic=off` 却配了 `--pid` 报错、`--dynamic=on` 但 application/pid 全空报错、两者同时配置报错，随后做与 dynamic 冲突的开关参数检查。文档表格里「--pid 在 attach 必选、launch 不选」的约束在这里落地为硬校验。
- [msprof_bin.cpp:L81](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/msprof_bin.cpp#L81)：入口处打印「Use 'quit' or 'q' to exit dynamic profiling.」——交互模式提示语的输出点，说明交互命令循环就挂在 main 所在进程内。

#### 4.4.4 代码实践

**实践目标**：通过读校验代码，反推 `--dynamic` 参数的合法命令组合（源码阅读型实践）。

**操作步骤**：

1. 精读 [input_parser.cpp:L1567-L1595](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/input_parser.cpp#L1567-L1595)，摘出全部 `CmdErrorLog("Argument ...")` 的报错条件。
2. 把每个报错条件翻译成一条「非法命令示例」。
3. 与 [dynamically.md:L15-L29](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/dynamically.md#L15-L29) 的注意事项逐条对照。

**需要观察的现象**：源码校验覆盖的非法组合数 ≥ 文档注意事项条数；注意源码校验不了的约束（如「launch 方式用户程序内不能设 PROFILING_MODE」）靠的是运行期覆盖（application.cpp）而非报错。

**预期结果**：得到一张「非法命令 → 报错信息 → 对应源码行」对照表。有昇腾环境时可任选一条非法命令实测，确认报错文案与源码字符串一致（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`--delay` 配了 10 秒，但业务程序 8 秒就跑完了，能采到数据吗？

**答案**：不能。[delayed_mode.md:L23](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/delayed_mode.md#L23) 明确「若配置的时间超过了 AI 任务的执行时间，在 AI 任务执行期间不会启动采集」；源码侧 [running_mode.cpp:L681-L683](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/msprofbin/src/running_mode.cpp#L681-L683) 会打「app process has exited」警告。

**练习 2**：`--delay=3 --duration=3` 中 duration 从什么时候开始计时？

**答案**：从 delay 结束的时刻开始。[delayed_mode.md:L24](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/delayed_mode.md#L24)：即第 3~6 秒是采集窗口，而不是从命令发起时算 3 秒。

**练习 3**：多卡集群训练想用动态采集，选 launch 还是 attach？为什么？

**答案**：attach。原因有二：其一，集群任务通常由启动脚本拉起，msprof launch 拿到的进程并非实际用卡进程（[dynamically.md:L11-L13](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/profiling/msprof_cmd/dynamically.md#L11-L13)）；其二，文档明确多卡（含集群）场景推荐 attach 方式。attach 前需在启动 AI 任务前 `export PROFILING_MODE=dynamic`。

## 5. 综合实践

**任务**：为一个训练脚本设计环境变量方式的采集配置并推演产物目录。

**重要前提说明（不要跳过）**：本讲的实践任务原始要求是「为 pytorch 训练脚本配置 PROFILING_MODE 与 PROFILING_OPTIONS」。但仓库文档明确写了这两个环境变量**仅适用于 TensorFlow 训练和在线推理场景**（[PROFILING_MODE.md:L17-L20](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/env-vars/PROFILING_MODE.md#L17-L20)、[PROFILING_OPTIONS.md:L282-L285](https://github.com/gitcode.com/cann-oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/docs/zh/env-vars/PROFILING_OPTIONS.md#L282-L285)）。因此本实践分两问：

**第一问（按文档适用范围）**：假设有一个 TensorFlow 训练脚本 `train.py`，写出完整的环境变量采集配置：

```sh
# 示例代码（依据 with_environment_variables.md 的配置示例改编）
export PROFILING_MODE=true
export PROFILING_OPTIONS='{"output":"/tmp/prof_training","training_trace":"on","task_trace":"l0","fp_point":"","bp_point":"","aic_metrics":"PipeUtilization","storage_limit":"200MB"}'
python3 train.py   # 训练结束后 /tmp/prof_training 下应出现 PROF_XXX/ 目录
```

然后画产物目录树：

```text
/tmp/prof_training/
└── PROF_数字_时间戳_随机串/      # 原始性能数据
    └── (解析后) mindstudio_profiler_output/   # 需另行用 msprof 解析命令生成
```

**第二问（回到 pytorch 的真实诉求）**：pytorch 脚本不能用环境变量方式，写出等价的正确做法——用 msprof 命令行包裹（本讲 4.1）：

```sh
# 示例代码（msprof 命令行方式，适用于任意框架）
msprof --output=/tmp/prof_pt /usr/bin/python3 train.py
```

若还想只采稳态段，再叠加延迟采集（4.4）：

```sh
msprof --delay=30 --duration=10 --output=/tmp/prof_pt /usr/bin/python3 train.py
```

**验收标准**：

1. 能说清为什么 pytorch 场景必须换方式（约束出处）。
2. `PROFILING_OPTIONS` 的 JSON 无引号错误（用 `python3 -m json.tool` 验证过 JSON 合法性——注意验证时要去掉 export 前缀和外围单引号）。
3. 两种方式的产物目录推演均符合本讲 4.2.4 / 4.1 的描述。有昇腾环境时上机验证并记录实际目录名格式；无环境时全部标注「待本地验证」。

## 6. 本讲小结

- msprof 有三类典型采集触发方式：**命令行**（`msprof [options] <app>`，任意框架通用）、**环境变量**（`PROFILING_MODE` + `PROFILING_OPTIONS`，仅 TensorFlow 训练/在线推理）、**acl.json / ACL API**（离线推理 C&C++ 程序），三者的采集项命名基本同构。
- `PROFILING_MODE` 三个取值：`true` 开采集（从 OPTIONS 读配置）、`false`/不配关闭、`dynamic` 是动态采集 attach 方式的前置配置；ACL/TF Adapter 接口的优先级高于该环境变量。
- `PROFILING_OPTIONS` 是 JSON 字符串，`output` 优先级高于 `ASCEND_WORK_PATH` 且目录自动创建；只开 MODE 不配 OPTIONS 时有五项默认采集集。
- **延迟采集是定时闹钟**（`--delay`/`--duration` 预先编排窗口，duration 从 delay 结束计时），**动态采集是手动快门**（`--dynamic=on` 进 `(msprof)` 交互模式敲 start/stop，launch 与 attach 两种接入），两者互斥不可同用。
- 文档参数与源码有精确对应：`dynamic`/`delay`/`duration` 定义在 `input_parser.cpp` 参数表并写入 `ProfileParams`；msprof 拉起子进程时会写入/覆盖 `PROFILING_MODE` 环境变量（`application.cpp`），这是「launch 方式下用户程序不能设 PROFILING_MODE」约束的源码根源。
- 每个文档参数都能在 C++ 入口找到唯一消费点——延续了 u1-l2 讲 build 体系时「shell 层参数在 CMake 层有唯一消费点」的同款工程审美。

## 7. 下一步学习建议

本讲是 msprof 单元（u4）的最后一篇用户视角讲义。建议：

1. **横向串读三份参数手册**：`PROFILING_OPTIONS`（环境变量）、acl.json 参数说明、msprof_cmd 各子页——三者采集项同构，对照阅读可一次记住一套语义。
2. **回到源码收官**：结合 u4-l2（msprofbin 入口）重读本讲 4.4.3 的校验代码，体会 `SplitApplicationArgv`（区分 msprof 选项与业务程序参数）如何支撑「方式一：命令末尾直接传 app」。
3. **进入下一单元 u5（hccl_test）**：如果说 msprof 测「算子跑得多快」，hccl_test 测「卡间通信跑得多快」，两者共同覆盖性能调优的两大维度。
4. 有昇腾环境且想深入解析侧的读者，可按文档指引跳转外部 `Ascend/msprof` 子仓的《使用 msprof 命令解析、查询与导出性能数据》，了解 PROF_XXX 原始数据如何变成可视化结果（该部分不在本仓库内）。
