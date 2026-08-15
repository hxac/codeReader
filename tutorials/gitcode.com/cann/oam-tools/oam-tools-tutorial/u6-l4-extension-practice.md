# 二次开发实战：为 oam-tools 贡献新功能

## 1. 本讲目标

本讲是整本学习手册的收官讲。前面五个单元分别精读了 asys、msaicerr、msprof、hccl_test 四个组件的内部实现，本讲把这些知识收拢到一件事上：**真的动手改这个仓库**。

学完本讲，你应该能够：

1. 拿到一个新需求，判断它应该改哪个组件、动哪个模块（还是根本不该改这个仓库）。
2. 掌握三个组件各自的「扩展点」及其最小改动集：
   - asys：新增一个采集项（三处改动：新包目录、入口函数、`collect()` 里一行调用）。
   - msprof：新增一个数据源插件（一个头文件，继承 `ProfPlugin`）。
   - hccl_test：新增一个集合通信算子测试（一对 `.h/.cc` + Makefile 两行）。
3. 为自己的改动配套编写 UT、更新文档，并跑通 u6-l2 讲过的本地门禁（pre-commit、OAT、增量代码检查）。
4. 规划一次完整的功能贡献：代码 + 测试 + 文档 + PR。

## 2. 前置知识

本讲默认你已读完依赖讲义 u2-l5（asys collect 子系统）、u4-l3（profapi 插件体系）、u5-l2（opbase 测试框架），并了解 u6-l1（测试体系）与 u6-l2（工程规范）。这里只把关键结论复述成「改造清单」的形态：

- **扩展点（extension point）**：框架代码中专门留给后来者插入自定义逻辑的位置。好的扩展点意味着「新增功能不需要改中心代码」，oam-tools 三个组件都用不同手法实现了这一点。
- **模板方法模式**：基类定好执行骨架，子类只覆写差异点。hccl_test 的 `HcclOpBaseTest` 是典型。
- **策略模式**：管理者持有一个接口指针，运行期可替换实现。msprof 的 `ProfPluginManager` 是典型。
- **隐式框架契约**：asys 采集子模块没有基类约束，只靠「目录名 + 入口函数签名 + 产物落 `dfx/`」的约定组织，新增模块只要遵守约定即可被接纳。
- **mock 测试**：asys 的 UT 用 pytest-mock 的 `mocker.patch` 把环境依赖（设备、外部命令）替换成假实现，使 UT 可在无昇腾硬件的环境运行（u6-l1）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/asys/collect/asys_collect.py` | asys 采集总调度 `AsysCollect.collect()`，新增采集项的注册点 |
| `src/asys/collect/ops/ops_collect.py` | 一个标准采集子模块的完整样例（搬运型） |
| `src/asys/collect/ops/__init__.py` | 子模块对外只暴露入口函数的约定写法 |
| `test/ut/asys/testcase/conftest.py` | asys UT 公共设施：源码路径注入、`AssertTest` 基类 |
| `test/ut/asys/testcase/collect/ops/test_ops_collect.py` | 采集子模块 UT 的标准写法（mocker 打桩） |
| `src/msprof/collector/dvvp/profapi/inc/prof_plugin.h` | msprof 数据源插件的纯虚基类契约 |
| `src/msprof/collector/dvvp/profapi/inc/prof_plugin_manager.h` | 插件管理者（策略模式的持有者） |
| `src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc` | opbase 测试的模板方法基类实现 |
| `src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.h` / `.cc` | 一个算子测试的完整样例（含工厂函数） |
| `src/hccl_test/Makefile` | 算子测试的注册点（`LIST` 与 `SRC` 映射） |

## 4. 核心概念与源码讲解

### 4.1 需求路由：新需求应该改哪里

#### 4.1.1 概念说明

oam-tools 是四个相对独立的工具共享一个构建体系，所以二次开发的第一步不是写代码，而是**判断需求归属**。判断错了组件，代码再对也会被评审打回。

#### 4.1.2 核心流程

```text
新需求进来
├── 是"收集故障现场信息"（日志/配置/栈/核心转储/新数据源）？ → asys（改 collect/ 或 analyze/）
├── 是"解析某种错误报告或 Dump 文件"？ → msaicerr（改 ms_interface/）
├── 是"采集新的性能数据类型"？ → msprof（改 collector/dvvp/profapi/ 或 analyze/）
├── 是"测试某个集合通信算子的正确性/带宽"？ → hccl_test（改 opbase_test/）
└── 都不是（是 CANN 本体功能）？ → 不属于本仓库，去对应 CANN 仓提 issue
```

三条经验法则：

1. **数据流向定组件**：信息「从设备流出到落盘」归 asys；「从文件流出到结论」归 msaicerr/msprof analyze；「从 API 调用流出到性能数字」归 hccl_test。
2. **扩展点定文件**：进入组件后，先找它的注册表（asys 的 `collect()` 调用序列、hccl 的 `LIST`、msprof 的 `ProfPluginManager`），注册表旁边就是扩展点。
3. **中心代码零改动是硬指标**：如果你的新增功能必须改公共基类或入口分发逻辑，先停下来重新设计——三个组件的既有设计都允许「只加文件、不改中心」。

#### 4.1.3 源码精读

asys 的「注册表」就是 `AsysCollect.collect()` 里那段顺序调用，每行 import + 一行调用就是一个采集项的注册位：

- [src/asys/collect/asys_collect.py:34-41](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/asys_collect.py#L34-L41)：`collect` 各子模块的 import 区，每个子模块只导入入口函数（如 `from collect.ops import collect_ops`）。
- [src/asys/collect/asys_collect.py:116-126](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/asys_collect.py#L116-L126)：`collect()` 中按固定顺序编排 graph、data_dump、ops、trace 四类采集，每个采集项就是一行 `collect_xxx(self.output_root_path)`——这一行就是你新增采集项时要插进去的位置。

hccl_test 的「注册表」在 Makefile：

- [src/hccl_test/Makefile:37](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L37)：`LIST` 变量列出全部 11 个测试目标，新增算子测试第一处注册。
- [src/hccl_test/Makefile:58](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L58)：`all_reduce_test: SRC = hccl_allreduce_rootinfo_test.cc` 把目标名映射到源文件，第二处注册。

#### 4.1.4 代码实践

1. **实践目标**：建立需求路由的直觉。
2. **操作步骤**：拿三个假想需求做归类练习——「a. 采集 NPU 进程打开的文件列表」「b. 解析一种新的性能原始文件 `.mytrace`」「c. 测量 ReduceScatterV 在 FP8 下的带宽」。
3. **需要观察的现象**：每个需求都能唯一落到某个组件的某个目录。
4. **预期结果**：a → asys `collect/`（新采集项）；b → 需先确认该文件由谁产出，若是 msprof collector 落盘的新格式则改 `src/msprof/collector/dvvp/analyze/`；c → 已有 `reduce_scatterv_test` 目标（见 Makefile `LIST` 第 8 项），只需检查 dtype 覆盖，可能**零改动**——「先查重再动手」本身就是路由的一部分。

#### 4.1.5 小练习与答案

**练习 1**：需求「解析 AI Core Error 报告时多提取一个字段」应改哪个组件？为什么？
**答案**：msaicerr（`src/msaicerr/ms_interface/aicore_error_parser.py`）。报告「解析」是 msaicerr 的职责，asys 只负责把报告采集到本地（u2-l7 讲过两者以子进程 + 目录交接解耦）。

**练习 2**：为什么「中心代码零改动」值得作为设计约束？
**答案**：它把新增功能的改动面收敛为新文件 + 一行注册，评审成本、合并冲突风险、回归风险都最小；同时强制新功能服从既有契约，防止框架被特例侵蚀。

### 4.2 asys collect 扩展点：新增一个采集项

#### 4.2.1 概念说明

u2-l5 已讲过 collect 子系统的框架契约，本节从「动手改」的角度复盘：asys 采集子模块没有基类、没有注册装饰器，靠三条约定接入：

1. **目录约定**：`src/asys/collect/<模块名>/<模块名>_collect.py`。
2. **入口约定**：模块级函数 `collect_<模块名>(output_root_path)`，产物落到 `output_root_path/dfx/<模块名>/` 下。
3. **失败语义约定**：单项采集失败只 `log_warning`，不抛异常、不中断其他采集项。

#### 4.2.2 核心流程

新增一个采集项（以「环境变量采集」为例）的完整流程：

```text
1. 新建 src/asys/collect/envvar/envvar_collect.py
   └── 定义 collect_envvar(output_root_path)
2. 新建 src/asys/collect/envvar/__init__.py
   └── from collect.envvar.envvar_collect import collect_envvar
3. 在 asys_collect.py 顶部加 import，在 collect() 序列中加一行调用
4. 新建 test/ut/asys/testcase/collect/envvar/test_envvar_collect.py（mocker 打桩）
5. （可选）更新 docs/zh/asys/ 下采集项说明文档
```

#### 4.2.3 源码精读

**注册点**——`collect()` 的调用序列（在 4.1.3 已引用，这里看它对子模块的错误语义）：

- [src/asys/collect/asys_collect.py:92-142](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/asys_collect.py#L92-L142)：`collect()` 全体。注意所有 `collect_xxx` 调用都没有 try/except 包裹——失败不中断的语义由各子模块**自己内部消化**（看 ops_collect.py L302-313：任何分支失败都只 `log_warning` 后返回 `None`/`False`），你的新模块也必须遵守这一点，否则一个坏数据源就会拖垮整个采集任务。

**标准样例**——ops 模块展示了入口函数的完整形态：

- [src/asys/collect/ops/ops_collect.py:302-313](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/ops/ops_collect.py#L302-L313)：`collect_ops(output_root_path)` 是唯一公开入口，内部先查 `check_launch_ops()` 开关、再按场景分流，所有失败路径只打告警。`__all__ = ["collect_ops"]`（L30）限定对外只暴露这一个名字。
- [src/asys/collect/ops/__init__.py:24](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/asys/collect/ops/__init__.py#L24)：包的 `__init__.py` 只有一行 re-export，这就是调用方能写 `from collect.ops import collect_ops` 的原因。

**UT 样例**——如何为采集项写免硬件测试：

- [test/ut/asys/testcase/conftest.py:39-40](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/asys/testcase/conftest.py#L39-L40)：`ut_root_path` 与 `ASYS_SRC_PATH`，UT 通过 `sys.path.insert` 把 `src/asys` 注入导入路径。
- [test/ut/asys/testcase/collect/ops/test_ops_collect.py:40-70](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/test/ut/asys/testcase/collect/ops/test_ops_collect.py#L40-L70)：`TestOpsCollect(AssertTest)` 继承 conftest 的测试基类（L207 定义 `AssertTest`），用 `mocker.patch` 把 `os.walk`、`FileOperate.collect_file_to_dir`、`ParamDict.get_command` 等全部依赖打桩后直调 `collect_ops("./output")` 断言行为——你的新采集项 UT 照这个模板写即可。

#### 4.2.4 代码实践

1. **实践目标**：完成 asys「环境变量采集项」的纸面二次开发（不改源码，产出设计稿与代码草稿）。
2. **操作步骤**：
   - 按上文流程写出 `envvar_collect.py` 草稿，核心逻辑约 15 行：遍历 `ASCEND_*` 前缀的环境变量，写入 `os.path.join(output_root_path, "dfx", "envvar", "env.txt")`，任何异常捕获后 `log_warning` 返回。
   - 写出 `__init__.py`（一行）与 `asys_collect.py` 的两行 diff（import + 调用位置建议放在 `collect_ops` 之后）。
   - 仿照 `test_ops_collect.py` 写 UT 草稿：用 `mocker.patch.dict(os.environ, {...})` 造环境，断言输出文件内容包含造出的键、不包含非 `ASCEND_` 前缀的键。
3. **需要观察的现象**（若有环境验证）：`bash build.sh -u --component asys` 后，新增 UT 被收集执行且通过。
4. **预期结果**：产出一份改动文件清单（4 个新文件 + 1 处两行修改 + 文档更新点 `docs/zh/asys/` 下采集项列表）。
5. 真实运行结果**待本地验证**（本环境无昇腾设备，UT 至少需要 pytest 与 pytest-mock 依赖，见 u6-l1）。

#### 4.2.5 小练习与答案

**练习 1**：如果新采集项需要 root 权限的命令（如 `lsof`），参考哪个既有模块的前置闸门写法？
**答案**：stacktrace 采集（u2-l5 讲过它有前置检查链与 Y/N 确认）。此外 asys diagnose 的硬件检测有 root/物理机闸门（u2-l6）。核心手法：先探测条件是否满足，不满足则 `log_warning` 后跳过，绝不抛异常。

**练习 2**：为什么 `collect_ops` 对外只暴露一个函数而不是一个类？
**答案**：该模块是「搬运型」采集，无跨步骤状态，函数即够；asys 框架对两种形态都接纳（stacktrace 是类形态 `AsysStackTrace`），选择依据是复杂度而非强制统一。

### 4.3 profapi 插件扩展点：新增一个数据源插件

#### 4.3.1 概念说明

u4-l3 讲过：当前开源仓的 profapi 目录只剩**头文件契约**（`.cpp` 实现已在提交 64ebbaa 中移出开源仓）。因此这里的「扩展」是**契约层扩展**——为新的数据源定义一个遵循 `ProfPlugin` 接口的插件头文件。它解决的问题是：不同设备形态/数据源的上报行为可以在运行期被替换，而接口层（`libmsprofiler.so`）不用重新编译。

#### 4.3.2 核心流程

```text
新增数据源插件：
1. 新建 src/msprof/collector/dvvp/profapi/inc/prof_mydata_plugin.h
2. class ProfMydataPlugin : public ProfPlugin，实现全部纯虚接口
   （生命周期：ProfInit / ProfStart / ProfStop / ProfFinalize；
     数据出口：ProfReportData / ProfReportApi / ProfReportEvent 等）
3. 在需要替换处经 ProfPluginManager::SetProfPlugin 注入；不注入则保持默认插件
4. 若新头文件进入安装面：更新对应 CMakeLists 的安装清单
```

#### 4.3.3 源码精读

- [src/msprof/collector/dvvp/profapi/inc/prof_plugin.h:40-48](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_plugin.h#L40-L48)：`ProfPlugin` 纯虚基类。生命周期四件套 `ProfInit/ProfStart/ProfStop/...` 与数据上报出口 `ProfReportData(moduleId, type, data, len)` 是新插件必须实现的接口；全部为纯虚（`= 0`），意味着**接口即合同**——少实现一个都编译不过，这是与 asys「隐式契约」相反的「显式契约」设计。
- [src/msprof/collector/dvvp/profapi/inc/prof_plugin_manager.h:21-30](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/msprof/collector/dvvp/profapi/inc/prof_plugin_manager.h#L21-L30)：`ProfPluginManager` 继承 Singleton，持有唯一 `profPlugin_` 指针，提供 `GetProfPlugin/SetProfPlugin`——u4-l3 总结的策略模式：默认惰性取默认插件，`SetProfPlugin` 可整体替换。你的新插件正是通过这个入口注入的。

#### 4.3.4 代码实践

1. **实践目标**：为一个假想的「自定义算子耗时数据源」写出插件头文件骨架。
2. **操作步骤**：
   - 通读 `prof_plugin.h` 全部纯虚方法签名，按「生命周期 / 数据出口 / 设备映射 / 杂项」分组抄成清单。
   - 写出 `prof_custom_op_plugin.h` 骨架：类声明 + 所有纯虚方法的空实现（`return 0;`），在 `ProfReportData` 的注释里写明你计划如何把自定义数据塞进无锁上报缓冲（参考 u4-l3 的 ReportBuffer 结论）。
3. **需要观察的现象**：只要有一个纯虚方法漏实现，编译即报错——用这一点反向核对你的清单是否完整。
4. **预期结果**：一个可编译的头文件骨架（可临时用一个最小 `.cc` include 它验证编译，验证后删除，不要提交）。
5. **待本地验证**：profapi 实现层不在开源仓内，插件的真实上报链路无法在本仓单独跑通；本实践的产出是接口设计稿，用于 PR 讨论。

#### 4.3.5 小练习与答案

**练习 1**：对比 asys 采集项（隐式契约）与 profapi 插件（纯虚接口显式契约）两种扩展设计的优劣。
**答案**：显式契约编译期强制完整实现、IDE 可导航，但接口演化受 ABI 稳定性约束（u4-l3 的 `prof_inner_api.h` extern "C" 门面即为隔离而设）；隐式契约零侵入、加文件即生效，但违约只能在运行期靠 review 与 UT 兜住。选择取决于模块的稳定性与贡献者群体。

**练习 2**：为什么 `ProfPluginManager` 用 Singleton 而不是全局函数？
**答案**：进程内插件必须全局唯一且惰性初始化（首次 `GetProfPlugin` 才确定默认插件）；Singleton 模板还统一了与仓库内其他单例（如 `OpDescParser`，u4-l4）的生命周期管理风格。

### 4.4 opbase 测试扩展点：新增一个集合通信算子测试

#### 4.4.1 概念说明

u5-l2 讲过三层继承体系与四个覆写点。本节把「新增一个算子测试」落成清单：hccl_test 的扩展点设计是三个组件中最机械的——**一对 `.h/.cc` + Makefile 两行**，公共骨架（参数解析、MPI 建链、数据量循环、计时打印）全部由基类 `HcclTest`/`HcclOpBaseTest` 提供。

#### 4.4.2 核心流程

```text
1. 新建 opbase_test/hccl_xxx_rootinfo_test.h
   └── class HcclOpBaseXxxTest : public HcclOpBaseTest，声明覆写点
2. 新建 opbase_test/hccl_xxx_rootinfo_test.cc
   ├── HcclTest* init_opbase_ptr(HcclTest*) { return new HcclOpBaseXxxTest(); }  // 工厂函数
   └── 实现 hccl_op_base_test()（主节奏）与各覆写点
3. Makefile：LIST 加目标名 xxx_test；加一行 xxx_test: SRC = hccl_xxx_rootinfo_test.cc
4. 文档：docs/zh/hccl_test/ 下补算子说明与用法（若适用）
```

#### 4.4.3 源码精读

**基类的「可覆写骨架」**：

- [src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc:41-44](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc#L41-L44) 与 [101-109](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc#L101-L109)：`hccl_op_base_test()`、`init_buf_val()`、`check_buf_result()` 在基类里是空实现——子类必须覆写才有意义，基类只保证「不覆写也能链接」。
- [src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc:46-99](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc#L46-L99)：`init_data_count()` 按 dtype 换算 count 与 type_size，全部 dtype 已收口在基类，新算子测试通常无需再碰。
- [src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc:111-133](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/common/src/hccl_opbase_rootinfo_base.cc#L111-L133)：`no_verification()`/`is_initdata_overflow()` 溢出保护。若你的新算子有特定溢出阈值，覆写 `is_data_overflow()` 时必须复用 `no_verification()` 而不是自己关 check。

**子类样例的「四覆写点 + 工厂函数」**：

- [src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.h:26-43](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.h#L26-L43)：类声明全景——覆写 `hccl_op_base_test`、`init_malloc_Ksize_by_data`、`init_send_recv_size_by_data`、`init_buf_val`/`check_buf_result`，正是 u5-l2 总结的四个差异收敛点。
- [src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc:31-36](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc#L31-L36)：工厂函数 `init_opbase_ptr`——所有 11 个二进制共享同一份 `hccl_test_main.cc` 的 main，靠链接期各自绑定的这个工厂实例化具体测试类（u5-l1）。
- [src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc:132-176](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/opbase_test/hccl_allreduce_rootinfo_test.cc#L132-L176)：主节奏完整样例——溢出判定 → 灌数 → 预热 → event 计时轮 → 重灌单跑校验 → `cal_execution_time` 打印。新算子测试的主体就是把其中的 `HcclAllReduce(...)` 换成你的 `HcclXxx(...)` 并调整校验期望。
- [src/hccl_test/Makefile:80-81](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/src/hccl_test/Makefile#L80-L81)：编译规则——每目标用 `$(Common_SRC)`（全部公共源码）+ 自己的一个 `.cc` 链成独立二进制。

#### 4.4.4 代码实践

1. **实践目标**：为一个尚未覆盖的集合通信变体（假想 `HcclGatherV`，带 root 的变长聚集）写出测试骨架。
2. **操作步骤**：
   - 抄 `hccl_allreduce_rootinfo_test.h` 的结构写 `hccl_gatherv_rootinfo_test.h`：`HcclOpBaseGathervTest : public HcclOpBaseTest`；带 root 的算子注意 u5-l2 结论——仅 root rank 生成期望值并校验（对照 `reduce_test` 的做法）。
   - `.cc` 里实现工厂函数 + `hccl_op_base_test()`（把 `HcclAllReduce` 换成假想的 `HcclGatherV`，并为每 rank 传入 counts/displacements 数组）+ `init_send_recv_size_by_data()`（gather 的 send/recv 尺寸随 rank 不同，这是与 allreduce 最大的差异点）+ `init_buf_val()`/`check_buf_result()`。
   - Makefile 两行：`LIST` 追加 `gatherv_test`，加 `gatherv_test: SRC = hccl_gatherv_rootinfo_test.cc`。
3. **需要观察的现象**：在装有 CANN 与 MPI 的环境执行 `make ASCEND_DIR=... MPI_HOME=...`，新目标出现在编译清单并产出 `bin/gatherv_test`。
4. **预期结果**：改动文件清单 = 2 个新文件 + Makefile 2 行 + `docs/zh/hccl_test/` 用法说明更新。注意：若 HCCL 本体尚无该 API，则本实践仅作骨架演练，`HcclGatherV` 处标注「示例代码」。
5. **待本地验证**（需要真实多卡环境）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `init_opbase_ptr` 用「每个 `.cc` 各自定义同名函数」而不是注册表？
**答案**：每个二进制只链接 `Common_SRC` + 一个算子 `.cc`（Makefile L80-81），同名工厂函数在链接期天然唯一绑定，无需运行期注册表，也避免了未链接算子的死代码——这是「链接期决定实现」的极简策略模式。

**练习 2**：新算子测试忘记调 `is_data_overflow()` 会怎样？
**答案**：大数据量 + 高精度损失 dtype（如 int8 × 多卡）时期望值溢出，逐元素比对会误报 failed。基类把它放在主节奏第一步（L132-134 附近）就是提醒：溢出判定是主流程的一部分，不可省略（u5-l3）。

## 5. 综合实践

**任务：完成一次「可以直提 PR」的功能贡献规划。**

从 4.2 / 4.4 两个可落地方向中任选一个（4.3 的 profapi 方向因实现层不在开源仓，仅适合接口讨论稿），完成以下交付物：

1. **改动文件清单**：每个文件一句话说明改什么（新增/修改，参考各节实践里给出的清单）。
2. **代码草稿**：asys 方向交付 `envvar_collect.py` + UT；hccl 方向交付 `.h/.cc` 骨架 + Makefile diff。
3. **测试计划**：写明用哪条命令验证——`bash build.sh -u --component asys`（asys UT，走纯 Python 快车道，u6-l1）或 `make ASCEND_DIR=... MPI_HOME=...` + `mpirun ... bin/xxx_test -b 8 -e 8 -f 4 -d 8 -r 0 -c 1`（hccl，u5-l3）。
4. **文档更新点**：asys 方向更新 `docs/zh/asys/` 采集项说明；hccl 方向更新 `docs/zh/hccl_test/` 的算子列表与命令示例。
5. **门禁自查**：对照 u6-l2 的检查清单——License 头（新文件必须有 Apache-2.0 + 华为版权头，可从任一既有文件复制）、pre-commit 钩子链通过、ruff 增量告警为零（E501 行宽 120、T201 禁 print 等）、OAT 对新文件的 License Header 校验通过。
6. **PR 描述**：按 CONTRIBUTING.md 要求，非简单修复先提 Issue 关联，PR 里写清背景、改动点、测试证据（UT 通过截图或日志）。

全部完成后，你就走完了一次「需求路由 → 扩展点定位 → 编码 → 测试 → 文档 → 门禁 → PR」的完整贡献闭环。

## 6. 本讲小结

- 需求路由三问：数据流向定组件、注册表定扩展点、「中心代码零改动」定设计质量。
- asys 采集扩展点 = 新包目录 + 入口函数 `collect_xxx(output_root_path)` + `collect()` 一行调用，失败只告警不中断；UT 用 mocker 打桩免硬件运行。
- profapi 插件扩展点 = 继承 `ProfPlugin` 纯虚基类的一个头文件，经 `ProfPluginManager::SetProfPlugin` 注入；开源仓内是契约层扩展，真实链路待实现层开源。
- opbase 测试扩展点 = 一对 `.h/.cc`（含工厂函数 `init_opbase_ptr`）+ Makefile 的 `LIST`/`SRC` 两行，差异收敛在四个覆写点。
- 一次合格贡献 = 代码 + UT + 文档 + 门禁（License 头、pre-commit、OAT、增量 codecheck）+ 关联 Issue 的 PR。
- 三个组件用三种手法实现同一目标——新增功能只加文件、不改中心：函数约定（asys）、纯虚接口（msprof）、链接期工厂（hccl_test）。

## 7. 下一步学习建议

本讲义是手册收官，此后建议以真实 Issue 驱动学习：

1. 到仓库的 Issue 列表找标记为入门级的缺陷或小需求，按本讲 4.1 的路由方法归类后实战一次。
2. 重读 [CONTRIBUTING.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/CONTRIBUTING.md) 与 [AGENTS.md](https://github.com/gitcode.com/cann/oam-tools/blob/047c299103c5830ddfc0b6ae6577137c1eb78a4d/AGENTS.md)，确认贡献流程细节（CLA 签署、分支命名、PR 模板）。
3. 想深入 msprof 实现层的读者，可用 u4-l3 介绍的方法 `git show 64ebbaa` 考古被移出开源仓的 profapi/analyze 实现，理解无锁上报缓冲的完整链路。
4. 关注 `docs/zh/design/` 目录：新组件的设计文档往往先于代码合入，是预判扩展点演化的最佳情报源。
