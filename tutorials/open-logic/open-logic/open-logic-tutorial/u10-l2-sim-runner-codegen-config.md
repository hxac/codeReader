# 仿真运行器、代码生成与测试配置

## 1. 本讲目标

本讲是第 10 单元「验证、工程化与 CI」的第二讲，承接 [u10-l1](u10-l1-vunit-tb-and-vcs.md)（VUnit 测试台结构与验证组件）。在上一讲里，我们把目光放在**测试台（TB）内部**：`runner_cfg`、`run("X")`、`-- vunit: run_all_in_same_sim`、验证组件（VC）。本讲把镜头拉远，聚焦**测试台之外、驱动整个仿真工厂运转的那一层**——`sim/run.py` 仿真运行器、`sim/codegen.py` 前置代码生成器，以及 `sim/test_configs/` 测试配置目录。

学完本讲，读者应该能够：

- 说清 `run.py` 是一段「配置脚本」而非测试代码，理解它从导入到 `vu.main()` 的完整流水线。
- 掌握仿真器选择机制：`--ghdl`/`--nvc`/`--modelsim`/`--rivierapro` 互斥开关与 `VUNIT_SIMULATOR` 环境变量的关系。
- 理解一个关键的执行时序：为什么代码生成必须在 VUnit 扫描源文件**之前**完成。
- 掌握 `--coverage`、`--compile_list`、`--vhdl_ls` 三个工程化选项的用途。
- 学会在 `test_configs/<area>.py` 中为一个实体新增一组 generic 配置，并理解一个 TB 是如何「展开」成成百上千个仿真实例的。

## 2. 前置知识

本讲默认读者已经掌握以下概念（来自前序讲义，这里只做最小回顾）：

- **VUnit**：一个开源的 VHDL 验证框架，负责「自动发现源文件、按依赖排序编译、把每个测试用例调度成一次独立仿真」。Open Logic 的整个仿真流程都构建在它之上（见 u1-l4、u10-l1）。
- **Generic（泛型）**：实例化时确定的参数，例如 `Depth_g => 32`。同一个 TB 实体配不同 generic 就是不同的测试用例。
- **库名 `olo` 与 `olo_tb`**：生产源码编译进 `olo` 库，测试台编译进 `olo_tb` 库，二者分离便于覆盖率统计（见 u1-l3、u1-l4）。
- **`named_config` / `add_config`**：上一讲（u10-l1）已初步接触——Python 侧把一组 generic 组合注册成一个「具名配置」。本讲会精读它的实现。
- **代码生成（codegen）**：用 Python 算出常量，再渲染成 VHDL 包文件（见 u8-l4）。本讲只关心它在仿真流程中的**调用时机**，不重复其内部原理。
- **协仿真 `pre_config`**：VUnit 允许在每个用例仿真前先跑一段 Python（如生成 `.fix` 激励文件，见 u8-l5）。

如果你对「为什么一个 TB 能展开成多个仿真实例」还不清楚，建议先读完 u10-l1 再回来。

一个贯穿全讲的直觉：**`run.py` 是工厂调度员，`codegen.py` 是开工前的备料工序，`test_configs/` 是订单清单**。调度员必须先让备料完成（否则机器找不到原料），再读订单清单决定要跑哪些仿真。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `sim/` 目录：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `sim/run.py` | 仿真总入口（VUnit 配置脚本） | CLI 选项、仿真器选择、库与源码收集、配置加载、`compile_list`/`coverage` 分支、`vu.main()` |
| `sim/codegen.py` | 前置代码生成器 | `generate()` 函数、生成 `pkg_writer_test_pkg.vhd`、为何必须在 VUnit 扫描前运行 |
| `sim/test_configs/utils.py` | 配置工具函数 | `named_config()` 如何把 generic 字典变成具名配置并调用 `add_config` |
| `sim/test_configs/olo_base.py` | base 区域测试配置 | `add_configs()` 如何为各实体批量注册 generic 组合 |

其余区域配置文件 `olo_axi.py` / `olo_intf.py` / `olo_fix.py` 结构与 `olo_base.py` 完全一致，本讲以 `olo_base.py` 为样本。

## 4. 核心概念与源码讲解

### 4.1 VUnit 集成与运行选项

#### 4.1.1 概念说明

`sim/run.py` 本身**不是测试代码，也不含任何 VHDL**。它是一段大约 200 行的 Python「配置脚本」：它构建一个 VUnit 项目对象，告诉 VUnit「源码在哪、测试台在哪、各自进哪个库、要跑哪些配置、用什么仿真器」，最后把控制权交给 `vu.main()`，由 VUnit 接管编译与仿真。

这种「用脚本描述工程、由框架执行」的模式，是 VUnit 的核心使用方式，也是 Open Logic 能做到「一行命令跑通全库上千个用例」的关键。

理解 `run.py` 的一个有效方法是把它看作一条**线性流水线**：导入 → 备料（codegen）→ 解析命令行 → 选仿真器 → 构建 VUnit 项目 → 加库 → 收源码 → 加配置 → （可选：导出编译清单）→ 设仿真选项 → （可选：覆盖率）→ 交棒 `vu.main()`。下面逐段精读。

#### 4.1.2 核心流程

```text
run.py 执行流水线
──────────────────────────────────────────────────────────
 ① 导入 test_configs 与 codegen            （第 11-12 行）
 ② codegen_generate()  备料生成 VHDL 包     （第 19 行）★ 关键时序
 ③ os.chdir 到 sim 目录                     （第 26 行）
 ④ 解析 CLI（仿真器/coverage/compile_list…）（第 28-73 行）
 ⑤ 设置 VUNIT_SIMULATOR 环境变量            （第 76-87 行）
 ⑥ VUnit.from_args + 内置库 + COM + VC      （第 107-110 行）
 ⑦ 建库 olo / olo_tb                        （第 113-114 行）
 ⑧ glob 收集 src + en_cl_fix 源码进 olo     （第 117-119 行）
 ⑨ glob 收集 test 台进 olo_tb               （第 126-127 行）
 ⑩ 加载四个区域的测试配置                   （第 138-139 行）
 ⑪ （若 --compile_list）导出编译清单并退出   （第 146-157 行）
 ⑫ 设仿真/编译选项、覆盖率钩子              （第 159-190 行）
 ⑬ （若 --vhdl_ls）生成语言服务器配置并退出  （第 193-197 行）
 ⑭ vu.main(post_run=post_run) 真正开跑      （第 200 行）
```

注意第 ⑪ 和 ⑬ 两步是「导出某样东西后立即 `exit(0)`」的旁路：它们不跑仿真，只产出工件（编译顺序文件、语言服务器配置）。这是 `run.py` 作为「多功能入口」的体现。

#### 4.1.3 源码精读

**先看导入与备料**。`run.py` 把测试配置拆到独立文件，并显式在构建 VUnit 对象之前调用代码生成：

[sim/run.py:L9-L19](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L9-L19) —— 导入 `test_configs` 四个区域模块与 `codegen.generate`，并在第 19 行立即调用 `codegen_generate()`。第 17 行的注释点明了时序原因：「代码生成测试必须在 VUnit 检测文件**之前**生成代码，因为这些文件必须真实存在，VUnit 才能发现它们」。这条时序是本讲的灵魂，4.3 节会展开。

**再看构建 VUnit 项目与加库**：

[sim/run.py:L107-L114](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L107-L114) —— `VUnit.from_args(args=args)` 把命令行参数转成项目对象；随后 `add_vhdl_builtins()`、`add_com()`、`add_verification_components()` 引入 VUnit 自带的标准库与消息机制（COM）和验证组件（VC，见 u10-l1）；最后建两个库 `olo` 与 `olo_tb`。

**收集源码**：用 `glob` 递归抓取所有 `.vhd` 文件，生产源码与第三方 `en_cl_fix` 一起进 `olo` 库，测试台进 `olo_tb` 库：

[sim/run.py:L117-L127](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L117-L127) —— 注意 `olo` 库同时包含 `../src/**/*.vhd` 与 `../3rdParty/en_cl_fix/hdl/*.vhd`，这正是 fix 区域依赖子模块的体现（见 u1-l3）；而 `olo_tb` 收集 `../test/**/*.vhd`，其中 legacy 辅助文件单独先加入。

**加载配置（订单清单）**：一行循环把四个区域的配置全部注册进 `olo_tb` 库：

[sim/run.py:L138-L139](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L138-L139) —— 遍历 `[olo_base, olo_axi, olo_intf, olo_fix]`，各调用其 `add_configs(olo_tb)`。为什么配置拆到独立文件？[sim/run.py:L9-L11](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L9-L11) 的注释回答了：「它们被放在单独文件里，以控制 `run.py` 的体量」。

**最后交棒**：一切就绪后交给 VUnit 主循环：

[sim/run.py:L200-L200](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L200-L200) —— `vu.main(post_run=post_run)` 真正开始编译与仿真；`post_run` 是仿真全部结束后的回调，4.1 节末尾会看到它在覆盖率场景下用来合并覆盖率数据。

#### 4.1.4 代码实践

**实践目标**：亲手把 `run.py` 跑起来，验证它「自动发现 TB + 按配置展开用例」的行为。

**操作步骤**：

1. 进入 `sim` 目录（`run.py` 第 26 行会自己 `chdir`，但从该目录运行最稳妥）。
2. 列出 VUnit **发现了哪些测试**而不真正编译运行——VUnit 用 `--list` 参数列出全部用例：

   ```bash
   cd sim
   python run.py --list
   ```

3. 观察输出里同一个 TB（例如 `olo_tb.olo_base_arb_prio_tb`）下挂了多个配置（`Latency_g=0`、`Latency_g=1`、`Latency_g=3`，来自 4.4 节的配置）。

**需要观察的现象**：

- `--list` 会打印形如 `olo_tb.olo_base_arb_prio_tb.Latency_g=0.run` 的全名——这正是 u10-l1 所说的「库.TB.配置.用例」四级全名。
- 用例总数远大于 TB 文件数，因为一个 TB 被 generic 组合「展开」成了很多实例。

**预期结果**：`--list` 输出包含数百乃至上千条用例（全库 base+axi+intf+fix 四个区域），且无需真正仿真即可看到。

> 待本地验证：具体用例条数取决于本地是否 `--recursive` 克隆了 `en_cl_fix`（否则 fix 区域 TB 缺依赖，数量会不同）。

#### 4.1.5 小练习与答案

**练习 1**：`run.py` 里 `add_com()` 与 `add_verification_components()` 各引入了什么？去掉它们会导致哪类测试台编译失败？

**参考答案**：`add_com()` 引入 VUnit 的消息通信机制（actor 信箱／net 网络），是验证组件（VC）之间收发事务的底层；`add_verification_components()` 引入 VUnit 自带的 AXI 等总线功能模型 VC（见 u10-l1）。去掉后，凡是在 TB 中实例化 VC 或使用消息机制的测试台都会因找不到 `vunit_lib` 中的相关单元而编译失败。

**练习 2**：为什么生产源码进 `olo` 库、测试台进 `olo_tb` 库，而不是都放进一个库？

**参考答案**：分离便于**覆盖率统计**——`--coverage` 模式只对 `olo` 库（生产代码）插桩（见 [sim/run.py:L173-L174](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L173-L174)），测试台自身不算被测对象。同时也让编译依赖更清晰：TB 依赖 `olo`，反之不成立。

---

### 4.2 仿真器选择机制

#### 4.2.1 概念说明

Open Logic 的「Pure VHDL、不依赖厂商原语」哲学（见 u1-l1）带来的直接红利是：**同一份 RTL 可以在多个仿真器上跑**。官方默认用开源的 **GHDL**（零成本，是 CI 免费检查的前提），同时支持 **NVC**、**ModelSim/Questa**、**Riviera-PRO**。

VUnit 本身支持多种「仿真器后端」（simulator interface），通过环境变量 `VUNIT_SIMULATOR` 选择。`run.py` 做的事情，就是把命令行开关翻译成这个环境变量——这样用户只需记 `--ghdl` 这类直观开关，而不必知道底层环境变量。

这里有两个容易踩坑的工程细节值得专门讲：**互斥开关**与 **Riviera-PRO 的泛型引号绕过**。

#### 4.2.2 核心流程

```text
命令行 → 互斥开关解析 → VUNIT_SIMULATOR 环境变量 → VUnit 后端
                          │
                          ├─ modelsim / nvc / ghdl / rivierapro
                          │
                          └─ 若 rivierapro：打补丁 format_generic，给含 ( ) , 空格 的值加引号
                          └─ 若 --coverage：仅允许 modelsim / nvc，否则抛异常
```

#### 4.2.3 源码精读

**互斥开关**：四个仿真器选项放进同一个 `add_mutually_exclusive_group()`，即命令行只能选一个；`--ghdl` 设了 `default=True`，故不指定时默认走 GHDL：

[sim/run.py:L28-L53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L28-L53) —— 注意 `--ghdl` 的 `default=True`。`--coverage`/`--vhdl_ls`/`--compile_list` 不在这个互斥组里，因为它们与仿真器正交。

**翻译成环境变量**：只有当用户**没有**预先设 `VUNIT_SIMULATOR` 时，才根据开关设值，否则尊重外部设定（方便 CI 注入）：

[sim/run.py:L76-L87](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L76-L87) —— `--rivierapro` 还会打印一条警告：Riviera-PRO「Open Logic 不再积极维护」。最终 `os.environ['VUNIT_SIMULATOR'] = simulator` 让 VUnit 在初始化时读到正确后端。

**Riviera-PRO 泛型引号绕过**：这是一个真实 bug 的修复，非常值得学习。VUnit 的 `format_generic` 只给「含空格」的泛型值加引号；但 Riviera-PRO 会把不带引号的 `(1,8,4)` 这种值**当成 VHDL 聚合表达式**而非字符串解析，导致拒绝并退回默认值。修复办法是给含 `(`、`)`、`,`、空格 的值都加上引号：

[sim/run.py:L89-L100](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L89-L100) —— 动态 `import` Riviera-PRO 后端模块，用猴子补丁（monkey-patch）替换其 `format_generic` 函数。这种「按需打补丁、不污染其他后端」的写法是处理工具差异的好范例。

**覆盖率兼容性检查**：`--coverage` 只在 modelsim 或 nvc 下成立，否则直接抛异常终止：

[sim/run.py:L103-L104](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L103-L104) —— 注意此处用的是局部变量 `simulator` 而非重新读环境变量，与第 78-86 行的赋值一致。

#### 4.2.4 代码实践

**实践目标**：体会「切换一个开关即换仿真器后端」，并观察环境变量如何被设置。

**操作步骤**：

1. 默认运行（GHDL），先只列出而不真正跑，确认默认后端是 GHDL：

   ```bash
   cd sim
   python run.py --list 2>&1 | head -20
   ```

2. 切到 NVC 列出：

   ```bash
   python run.py --nvc --list 2>&1 | head -20
   ```

3. 故意制造一个非法组合，观察保护性异常：

   ```bash
   python run.py --ghdl --coverage   # 应抛 "Coverage is only allowed with --modelsim or --nvc."
   ```

**需要观察的现象**：第 3 步会因 [sim/run.py:L103-L104](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L103-L104) 的检查而立即报错退出，不会进入漫长编译。

**预期结果**：默认与 `--nvc` 都能正常列出用例；`--ghdl --coverage` 抛异常。

> 待本地验证：本机需已安装对应仿真器才能真正编译仿真；`--list` 只需 VUnit 能初始化后端即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么四个仿真器开关要用「互斥组」而不是四个普通开关？

**参考答案**：因为同一时刻只能用一个仿真器后端，`VUNIT_SIMULATOR` 只能取一个值。若做成普通开关，用户可能同时给 `--ghdl --nvc`，代码里的 `if/elif` 会悄悄取一个而忽略另一个，造成「我以为在跑 NVC，其实在跑 GHDL」的隐蔽错误。互斥组让 argparse 在命令行层面就拒绝这种组合，错误前置、可见。

**练习 2**：如果 CI 想固定用 Questa 跑覆盖率，除了命令行 `--modelsim --coverage`，还可以怎么做？

**参考答案**：可以直接在 CI 环境里预设 `VUNIT_SIMULATOR=modelsim`（或 `questasim`，视 VUnit 版本），这样即使不加 `--modelsim` 也会走该后端——因为 [sim/run.py:L76](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L76-L76) 的 `if 'VUNIT_SIMULATOR' not in os.environ` 会跳过覆盖。`--coverage` 仍需显式给出。

---

### 4.3 前置代码生成（codegen）

#### 4.3.1 概念说明

Open Logic 的 fix 区域有一部分 VHDL 包**不是人手写的，而是 Python 生成出来的**（见 u8-l4 的 `olo_fix_pkg_writer`）。生成的产物（如 `pkg_writer_test_pkg.vhd`）是真实的 `.vhd` 文件，会被某个 TB `use` 引用。

这就引出一个**鸡生蛋问题**：VUnit 在启动时会**扫描磁盘上的源文件**来建立依赖图。如果生成包此刻还不存在，VUnit 就找不到它，引用它的 TB 会编译失败。因此代码生成必须抢在 VUnit 扫描**之前**完成。

`sim/codegen.py` 就是这段「备料」工序的实现。它在 `run.py` 第 19 行被无条件调用，且位置极其靠前——甚至在 `os.chdir` 和 CLI 解析之前。

#### 4.3.2 核心流程

```text
run.py 启动
   │
   ├─ ① codegen_generate()            ← 必须最先
   │       └─ codegen.py: generate()
   │             ├─ chdir 到 sim
   │             ├─ olo_fix_pkg_writer 累加常量/向量
   │             └─ write_vhdl_pkg → 写出 pkg_writer_test_pkg.vhd
   │
   ├─ ② os.chdir / 解析 CLI / 构建 VUnit 项目
   │
   └─ ③ VUnit 扫描源文件（此刻生成包已存在于磁盘）✓
```

关键：第 ① 步写盘完成后，第 ③ 步 VUnit 才扫描。顺序不能颠倒。此外，生成包是**构建产物**，被 `.gitignore` 忽略（匹配 `*_pkg.vhd` 规则），每次仿真都重新生成，保证它与 Python 单一真相源一致（详见 u8-l4）。

#### 4.3.3 源码精读

**`run.py` 中的调用点**——注意它在文件最顶端、所有 VUnit 相关代码之前：

[sim/run.py:L11-L19](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L11-L19) —— 第 12 行 `from codegen import generate as codegen_generate`，第 19 行 `codegen_generate()`。此刻 `VUnit` 对象尚未创建（第 107 行才创建），扫描自然还没发生。

**`codegen.py` 的实现**：它把 `src/fix/python` 加入 `sys.path`，导入 `olo_fix_pkg_writer` 与 `en_cl_fix_pkg`，然后累加一批常量与向量，最后渲染成 VHDL 包：

[sim/codegen.py:L7-L10](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/codegen.py#L7-L10) —— `sys.path.append` 指向 `../src/fix/python`，再 `from olo_fix import olo_fix_pkg_writer`、`from en_cl_fix_pkg import *`（后者提供 `FixFormat` 类型）。

[sim/codegen.py:L16-L39](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/codegen.py#L16-L39) —— `generate()` 先 `chdir` 到 sim 目录（第 18 行，与 `run.py` 同样的「从自身位置定位」约定），创建 `pkg_writer`，逐条 `add_constant`/`add_vector`（第 22-37 行，含原生与 `as_string=True` 两套），最后 `write_vhdl_pkg("pkg_writer_test_pkg", "../test/fix/olo_fix_pkg_writer", olo_library="olo")`（第 39 行）写出文件。

`olo_library="olo"` 参数确保生成的包 `use olo.olo_base_pkg_array.all` 等 `use` 语句指向正确的库名（见 u8-l4）。

`codegen.py` 也可独立运行（`__main__`，第 41-42 行），方便单独调试生成结果而无需启动整个 VUnit。

#### 4.3.4 代码实践

**实践目标**：亲手验证「生成必须在扫描之前」这条时序——通过人为破坏它来观察后果。

**操作步骤**：

1. 先确认生成产物存在与内容：

   ```bash
   cd sim
   python codegen.py        # 单独跑一次代码生成
   ls -l ../test/fix/olo_fix_pkg_writer/
   head -20 ../test/fix/olo_fix_pkg_writer/pkg_writer_test_pkg.vhd
   ```

2. 删掉生成包，再尝试列测试（VUnit 会重新走扫描阶段）：

   ```bash
   rm ../test/fix/olo_fix_pkg_writer/pkg_writer_test_pkg.vhd
   python run.py --list 2>&1 | head -40
   ```

3. 再次正常 `python run.py --list`（这次 `run.py` 第 19 行会先 `codegen_generate()` 把文件写回来）。

**需要观察的现象**：

- 第 2 步删除生成包后，如果 VUnit 在扫描阶段就需要它，可能报「找不到 `pkg_writer_test_pkg`」之类的错误；而第 3 步因为 `run.py` 顶部的 `codegen_generate()` 已把文件写回，应恢复正常。
- 这正反两面共同证明：`codegen_generate()` 的位置（[sim/run.py:L19](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L19-L19)）是功能正确的必要条件。

**预期结果**：第 2 步出现编译/发现错误；第 3 步成功列出。

> 待本地验证：VUnit 对「缺失文件」的报错时机取决于版本——可能在 `--list` 阶段、也可能在真正编译阶段才暴露。无论哪个阶段，根因都是「生成包晚于扫描出现」。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `run.py` 第 19 行的 `codegen_generate()` 删除，并依赖开发者手动先跑 `python codegen.py`，会有什么隐患？

**参考答案**：会破坏「单一入口、自动备料」的便利，且极易出错——开发者（或 CI）很可能忘记先跑 `codegen.py`，导致 VUnit 扫描时找不到生成包而编译失败。把它放在 `run.py` 顶部无条件调用，正是为了让备料对使用者透明、不可遗忘。

**练习 2**：生成的 `pkg_writer_test_pkg.vhd` 为什么不入版本库（被 `.gitignore` 忽略）？

**参考答案**：因为它是**可由 Python 单一真相源完全重建的构建产物**，入版本库会带来双份维护（Python 改了还得记得同步改 VHDL），且容易在 PR 里制造无意义 diff。让它每次仿真重新生成，既保证一致，又保持版本库干净（见 u8-l4「单一真相源」）。

---

### 4.4 test_configs 按区域组织 generic 组合

#### 4.4.1 概念说明

Open Logic 全库有上千个仿真实例，但 TB 文件远没有那么多——因为**一个 TB 实体配不同 generic 就是不同的测试用例**。把这些 generic 组合集中、系统地登记起来，就是 `test_configs/` 目录的职责。

它的组织方式很优雅：

- 按区域拆文件：`olo_base.py` / `olo_axi.py` / `olo_intf.py` / `olo_fix.py`，每个文件导出一个 `add_configs(olo_tb)` 函数。
- 每个文件内部，按实体分块，用 `for` 循环批量登记 generic 组合（例如把 `RamBehavior_g` 在 `['RBW','WBR']` 上各跑一遍）。
- 所有登记最终都经过同一个工具函数 `named_config()`，它把 generic 字典翻译成「人类可读的配置名」并调用 VUnit 的 `tb.add_config()`。

这种写法把「我要覆盖哪些参数组合」表达成**数据（列表）+ 循环**，而不是成百上千行手写配置，既紧凑又不易漏。

#### 4.4.2 核心流程

```text
run.py:  for area in [olo_base, olo_axi, olo_intf, olo_fix]:
             area.add_configs(olo_tb)          ← 入口

olo_base.py: add_configs(olo_tb):
   tb = olo_tb.test_bench('olo_base_arb_prio_tb')     ← 按名取出 TB 对象
   for Latency in [0, 1, 3]:
       named_config(tb, {'Latency_g': Latency})       ← 登记一组 generic

utils.py: named_config(tb, map):
   cfg_name = "Latency_g=3"                           ← 由字典拼出配置名
   tb.add_config(name=cfg_name, generics=map, ...)    ← 交给 VUnit

VUnit: 把 olo_tb.olo_base_arb_prio_tb.Latency_g=3 展开成一个独立仿真实例
```

注意最后一步：VUnit 收到 `add_config` 后，会把「这个 TB + 这组 generic」注册成一个独立仿真。一个 TB 若有多个 `run("X")` 用例（见 u10-l1），则每个配置下还会再乘以用例数。

#### 4.4.3 源码精读

**`utils.py` 的 `named_config`——整个配置体系的枢纽**：

[sim/test_configs/utils.py:L15-L21](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/utils.py#L15-L21) —— 第 16 行用字典推导把 `{'Latency_g': 3}` 拼成配置名 `"Latency_g=3"`，多键则用 `-` 连接（如 `"RamBehavior_g=RBW-IsAsync_g=True"`）；第 21 行调用 VUnit 的 `tb.add_config(name=cfg_name, generics=map, pre_config=pre_config)`。`pre_config` 参数是协仿真的钩子（见 u8-l5，每个用例仿真前先跑 Python 生成 `.fix` 激励），本讲不展开。`short_name` 可覆盖自动拼名。

**`olo_base.py` 的入口与导入**：

[sim/test_configs/olo_base.py:L10-L16](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L10-L16) —— 相对导入 `from .utils import named_config`（`test_configs` 是包，靠 `run.py` 的 `from test_configs import ...` 进入包上下文）；`add_configs(olo_tb)` 接收 TB 库对象。

**典型模式一：批量时钟比组合（跨时钟域实体）**——多个 TB 共用同一套 generic 扫描：

[sim/test_configs/olo_base.py:L22-L34](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L22-L34) —— 先用列表 `cc_tbs` 列出所有 `olo_base_cc_*` TB，外层 `for tb_name` 复用同一段逻辑；内层把 7 组时钟比 `(N,D)` 各登记一次，并跳过 `N==D 且 N!=1` 的重复（「同频只仿真一次」）。`SyncStages_g` 在 `[2,4]` 上再各登记一次。

**典型模式二：多泛型交叉（RAM 实体）**——展示如何系统覆盖多种配置维度：

[sim/test_configs/olo_base.py:L52-L74](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L52-L74) —— 对三种 RAM TB，分别在 `RamBehavior_g`、`RdLatency_g`、`Width_g`、`UseByteEnable_g`、`InitFormat_g` 上循环登记；还针对 `olo_base_ram_sdp_tb` 额外交叉 `IsAsync_g`（第 58-60 行）。注意第 72-74 行的注释「Check init, byte-enables play a role internally」解释了为何要选 `[8,16]` 宽度配字节使能——配置选择背后是对**内部实现分支**的覆盖意图（承接 u2-l3 的 RBW/WBR 与字节使能拆分）。

**典型模式三：简单单泛型（arb_prio，本讲实践的样板）**：

[sim/test_configs/olo_base.py:L188-L192](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L188-L192) —— `olo_base_arb_prio_tb` 只在 `Latency_g` 的 `[0,1,3]` 上各登记一次。这是最适合练手的简单实体。注意紧随其后的 `olo_base_arb_rr`（第 194-196 行）注释「Only one config required, hence no add_config looping」——说明当 TB 用默认 generic 就够时，连 `add_config` 都不必调用，VUnit 会自动用 TB 实体里声明的默认值跑一个配置。

#### 4.4.4 代码实践

**实践目标**：为 `olo_base_arb_prio_tb` 新增一组 generic 配置 `Latency_g=2`，并验证它被 VUnit 发现、执行。

**操作步骤**：

1. 在 [sim/test_configs/olo_base.py:L191-L192](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L191-L192) 的 `for Latency in [0, 1, 3]:` 循环里加入 `2`，改为 `for Latency in [0, 1, 2, 3]:`（或在循环外单独加一行 `named_config(tb, {'Latency_g': 2})`）。

2. 先用 `--list` 确认新配置已被发现：

   ```bash
   cd sim
   python run.py --list 2>&1 | grep arb_prio
   ```

   预期看到 `olo_tb.olo_base_arb_prio_tb.Latency_g=2.run` 这一条新用例。

3. 只跑这一个新用例（VUnit 支持按全名过滤）：

   ```bash
   python run.py "olo_tb.olo_base_arb_prio_tb.Latency_g=2"
   ```

**需要观察的现象**：

- 第 2 步 `--list` 输出比改动前多出 `Latency_g=2` 一条，证明 Python 侧的 `named_config` → `add_config` 链路确实把新 generic 注册成了独立仿真实例。
- 第 3 步该用例通过（TB 对 `Latency_g` 是参数化的，值 2 与 0/1/3 同属合法自然数，见 [test/base/olo_base_arb_prio/olo_base_arb_prio_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_arb_prio/olo_base_arb_prio_tb.vhd) 中 `Latency_g : natural := 1`）。

**预期结果**：新用例被发现且仿真通过。

> 待本地验证：具体 VUnit 的过滤语法在不同版本可能为 `*Latency_g=2*` 通配；若直连全名无效，改用通配形式。

#### 4.4.5 小练习与答案

**练习 1**：`named_config(tb, {'RamBehavior_g': 'RBW', 'IsAsync_g': True})` 生成的配置全名是什么？它对应 VUnit 的哪一级？

**参考答案**：配置名为 `RamBehavior_g=RBW-IsAsync_g=True`（多键用 `-` 连接，见 [utils.py:L16](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/utils.py#L16-L16)）。它对应「配置」这一级；完整仿真实例全名还要再拼上「库.TB.…配置.用例」，例如 `olo_tb.olo_base_ram_sdp_tb.RamBehavior_g=RBW-IsAsync_g=True.run`（四级，见 u10-l1）。

**练习 2**：若一个 TB 只想用默认 generic 跑一次，需要写 `named_config` 吗？为什么？

**参考答案**：不需要。如 [olo_base.py:L194-L196](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L194-L196) 所示，`olo_base_arb_rr` 就没调用 `add_config`——VUnit 会自动用 TB 实体里声明的 generic 默认值注册一个默认配置。只有需要覆盖默认值、或同一 TB 要跑多组参数时，才需要 `named_config`。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「新增配置 → 运行 → 导出编译顺序」的完整工程化闭环。

**任务**：为 base 区域某个实体新增测试覆盖，并产出一份编译顺序工件。

**步骤**：

1. **备料理解**：阅读 [sim/codegen.py:L16-L39](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/codegen.py#L16-L39)，确认 `codegen_generate()` 会在 `run.py` 启动时最先执行，写出 `pkg_writer_test_pkg.vhd`。

2. **新增配置**：编辑 [sim/test_configs/olo_base.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py)，仿照 4.4.4 的做法，为 `olo_base_arb_prio_tb` 增加 `Latency_g=2`（或挑一个你感兴趣的单泛型实体，例如 `olo_base_strobe_gen_tb` 增加一个 `FreqStrobeHz_g` 值）。注意 `named_config` 接收的是 generic **名=>值** 字典，键名必须与 TB 实体里声明的 generic 完全一致。

3. **验证发现与执行**：

   ```bash
   cd sim
   python run.py --list 2>&1 | grep <你的实体名>      # 应看到新配置
   python run.py "<新配置的全名>"                       # 应仿真通过
   ```

4. **切换仿真器复跑**（可选，验证 4.2）：若有 NVC，用 `python run.py --nvc "<新配置全名>"` 再跑一次，确认 RTL 与仿真器无关。

5. **导出编译顺序**（验证 4.1 的 `--compile_list` 旁路）：

   ```bash
   python run.py --compile_list
   cat ../compile_order.txt | head -10
   wc -l ../compile_order.txt
   ```

   预期：`run.py` 走 [L146-L157](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L146-L157) 分支，按 VUnit 解析出的依赖顺序写出根目录的 `compile_order.txt`（约 93 行，只含 `olo` 库文件），随后 `exit(0)` 不跑仿真。

**验收标准**：

- `--list` 能看到你新增的配置全名。
- 单独跑该配置通过（若 TB 对该 generic 值合法）。
- `--compile_list` 成功生成 `compile_order.txt`，且包文件（如 `olo_base_pkg_array.vhd`）排在用到它的实体之前。

> 待本地验证：完整仿真需本机装有 GHDL（默认）或 NVC；`--list` 与 `--compile_list` 只需 VUnit 能初始化即可。

## 6. 本讲小结

- `sim/run.py` 是一段 **VUnit 配置脚本**，按「导入 → 备料 → 解析 CLI → 选仿真器 → 建库收源码 → 加配置 → 交棒 `vu.main()`」的线性流水线组织，本身不含 VHDL。
- **仿真器选择**靠四个互斥开关（默认 `--ghdl`）翻译成 `VUNIT_SIMULATOR` 环境变量；`--coverage` 仅兼容 modelsim/nvc；Riviera-PRO 有专门的泛型引号猴子补丁。
- **代码生成是无条件的前置工序**：`codegen_generate()` 必须在 VUnit 扫描源文件之前把生成包写盘，否则引用它的 TB 编译失败——这是本讲最关键的时序约束。
- `--compile_list` / `--vhdl_ls` 是「导出工件后 `exit(0)`」的旁路，不跑仿真。
- `test_configs/` 按区域拆文件，每个 `add_configs(olo_tb)` 用「列表 + 循环」批量登记 generic 组合，全部经由 `named_config`（[utils.py:L15-L21](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/utils.py#L15-L21)）把 generic 字典翻译成具名配置并调用 `tb.add_config`，使一个 TB 展开成大量仿真实例。
- 配置选择背后是对**内部实现分支**的覆盖意图（如 RAM 的 RBW/WBR、字节使能、各种宽度），不仅是「多跑几个数」。

## 7. 下一步学习建议

- **继续第 10 单元**：下一讲 [u10-l3 覆盖率分析、问题分析与质量徽章](u10-l3-coverage-issues-badges.md) 会讲解 `--coverage` 收集到的数据如何被 `sim/AnalyzeCoverage.py` 解析、按文件判断是否达 95% 阈值，以及 `AnalyzeIssues.py` 与 `Badge.py` 如何形成 CI 质量闭环——本讲的覆盖率钩子（[run.py:L172-L190](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/run.py#L172-L190)）正是它的数据来源。
- **若关心代码检查与综合自动化**：可直接跳到 [u10-l4 代码检查与综合测试自动化](u10-l4-lint-and-synthesis-test.md)，看 `lint/script` 与 `tools/inference_test` 如何与仿真并列为另一条质量管线。
- **深入 codegen 内部**：本讲只讲了 codegen 的调用时机，其渲染原理（Jinja2 模板、`as_string`、单一真相源）在 [u8-l4](u8-l4-python-codegen-pkg-writer.md) 已详述，建议回看以理解 `olo_library="olo"` 参数如何影响生成包的 `use` 语句。
- **建议阅读的源码**：把 `sim/run.py` 从头到尾通读一遍（仅 200 行），再对照 `sim/test_configs/olo_fix.py`（最大的配置文件）体会 fix 区域 generic 组合的复杂度，巩固本讲的配置组织模式。
