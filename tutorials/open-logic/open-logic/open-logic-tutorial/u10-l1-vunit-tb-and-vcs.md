# VUnit 测试台结构与验证组件

> 本讲是「验证、工程化与 CI」单元（第 10 单元）的第一讲。它把前面零散见过的 testbench（TB）与验证组件（VC）整理成一套**可复用、可参数化、可回归**的验证方法论。前置认知：u1-l4（VUnit + GHDL 的运行流程、`runner_cfg`、`test_configs` 分区）、u2-l4（同步 FIFO 的接口与 TB 现象）。

---

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出一个 Open Logic 测试台的**标准骨架**：`runner_cfg` 泛型、`test_runner_setup`、`while test_suite` 循环、`run("...")` 分支、`check_equal` 断言、`test_runner_cleanup`，以及 `-- vunit: run_all_in_same_sim` 注释的作用。
- 理解**泛型参数化测试用例**：TB 自带泛型，Python 侧 `test_configs` 通过 `named_config` 把不同泛型组合注册成多个具名配置，VUnit 逐一编译运行。
- 理解**验证组件（VC）**：它是一个用 VUnit 消息机制（actor / net / `push_*` / `expect_*`）驱动的可复用总线功能模型，如何实例化、如何混用多个 VC、如何 `wait_until_idle` 同步。
- 掌握 VC 的**命名约定**：`olo_test_*_vc`、`snail_case` 标识符、放在 `test/tb/`、在 TB 中以 Open-Logic 大小写风格实例化。
- 学会**动手仿写**一个带两个泛型参数化用例、并用一个 VC 做检查的测试台。

---

## 2. 前置知识

本讲假设你已经了解：

- **VUnit**：一个开源的 VHDL 验证框架，负责「编译源码 → 发现 TB → 按配置调度仿真器逐个跑用例 → 汇总通过/失败」。Open Logic 全库的仿真都建立在它之上（见 u1-l4）。
- **testbench（TB，测试台）**：一段不可综合的 VHDL 顶层，用来给被测器件（DUT, Device Under Test）施加激励、检查输出。它本身不会被综合成硬件。
- **泛型（generic）**：实例化时（对 TB 而言是「运行用例时」）才确定的参数，如 `Depth_g`、`Width_g`。
- **AXI-Stream 握手**：`Valid`/`Ready` 反压约定（见 u2-l2、u2-l4）。
- **actor / 消息传递（message passing）**：VUnit 的通信原语。一个 `actor` 是一个信箱（mailbox），TB 通过 `net`（network）向它投递消息，VC 内部的进程接收消息并据此驱动或检查引脚。这是本讲会新引入的概念，下面会展开。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd) | **标准 TB 骨架范本**：手写激励 + `check_equal`，不使用 VC。用来讲 TB 结构与 `run_all_in_same_sim`。 |
| [test/tb/olo_test_axi_master_vc.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_axi_master_vc.vhd) | **AXI 主机 VC**：典型的「包 + 实体」双段式 VC，演示 actor / `push_*` / `expect_*` / `wait_until_idle`。 |
| [test/tb/olo_test_fix_checker_vc.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_fix_checker_vc.vhd) | **定点检查器 VC**：按文件逐拍比对输出的「纯检查」型 VC，演示 VC 作为 checker 的用法。 |
| [test/tb/olo_test_activity_pkg.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_activity_pkg.vhd) | **TB 工具包**：`check_no_activity`、`wait_for_value`、`pulse_sig` 等可复用过程，TB 里最常用的辅助函数集合。 |
| [sim/test_configs/olo_base.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py) | **泛型参数化配置**：为 base 区域各 TB 注册多组泛型组合（`named_config`）。 |
| [sim/test_configs/utils.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/utils.py) | `named_config` 辅助函数：把泛型字典翻译成具名配置。 |
| [test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd) | **VC 实战范本**：在 TB 里实例化 AXI 主机 VC、用 `push_single_write`/`expect_single_read` 驱动并检查、`wait_until_idle` 收尾。 |
| [test/fix/olo_fix_round/olo_fix_round_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_round_tb.vhd) | **多 VC 混用范本**：同时挂 stimuli（激励）VC 与 checker（检查）VC，演示双 VC 协作。 |
| [doc/Conventions.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md) | **VC 命名约定**（第 70–100 行）：`snail_case`、放 `test/tb/`、实例化用 Open-Logic 风格。 |

---

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：**4.1 VUnit 测试台结构**、**4.2 泛型参数化用例**、**4.3 验证组件 VC**、**4.4 VC 命名约定**。

### 4.1 VUnit 测试台结构

#### 4.1.1 概念说明

一个 VUnit 测试台本质上是一个**状态机**：它知道当前要跑哪个用例、用例之间如何切换、何时结束。VUnit 把这套状态机的细节藏在 `runner` 这个隐藏变量里，只暴露几个宏式的过程让你调用。你需要做的只有两件事：

1. 在实体里声明一个**固定名字**的泛型 `runner_cfg : string`——VUnit 会把「当前用例名、输出路径、仿真器、是否启用覆盖率……」打包成这个字符串注入进来。
2. 在控制进程里按固定顺序调用：`test_runner_setup` → `while test_suite` 循环（内部用 `run("名字")` 区分用例）→ `test_runner_cleanup`。

这套骨架的好处是：**一个 TB 文件可以承载任意多个测试用例**，用例之间共享 DUT 实例化与时钟，只在 `run("...")` 分支里写各自不同的激励与检查。这样既复用了接线，又把用例彼此隔离。

#### 4.1.2 核心流程

一个标准 TB 的执行流程可以画成：

```
test_runner_setup(runner, runner_cfg)   ← 解析 runner_cfg，初始化状态机
            │
            ▼
   ┌──→ while test_suite loop            ← 还有用例没跑？是
   │        │
   │        ├─ 公共初始化（复位、等时钟）
   │        │
   │        ├─ if    run("Reset")          then ...   ← 仅当前激活用例的分支为真
   │        ├─ elsif run("TwoWords...")    then ...
   │        ├─ elsif run("WriteFullFifo")  then ...
   │        └─ ...
   │        │
   └────────┘
            │
            ▼
   test_runner_cleanup(runner)           ← 通知 VUnit 本仿真结束
```

其中两个关键点：

- **`run("X")` 不是「执行用例」**，而是一个布尔函数：当 VUnit 当前激活的用例名为 `"X"` 时返回真。所以多个 `if run(...)` 分支里**只有命中的那一个**会执行其激励。把所有分支放进 `while test_suite` 循环，状态机会在每个用例开始前把激活名切换好。
- **`-- vunit: run_all_in_same_sim`**：写在实体声明前一行的一条**注释指令**。默认情况下 VUnit 为每个用例启动一次独立仿真（重新编译、重新 elaborate）；加上这条注释后，**同一个 TB 的所有用例在同一个仿真进程里依次跑完**。因为初始化（elaborate、库加载）只做一次，回归速度大幅提升——代价是各用例共享同一个仿真上下文，因此**每个用例开头必须自己重新复位 DUT**，不能假设上一个用例留下的状态。

还有一个安全网：`test_runner_watchdog(runner, 10 ms)`——如果某个用例因为激励写错（例如死等一个永远不来的握手）卡住超过 10 ms，看门狗会强制结束并报失败，避免整个回归被一个挂死的用例拖住。

#### 4.1.3 源码精读

`olo_base_fifo_sync_tb` 是「纯手写激励、不用 VC」的标准骨架，最适合用来读结构。

实体声明：注意第 25 行的注释指令和第 28 行**必须叫 `runner_cfg`** 的泛型，其余泛型（`Depth_g`、`RamBehavior_g`、`ReadyRstState_g` 等）就是 4.2 要讲的参数化入口：

[test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd:25-35](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L25-L35) — 实体声明：第 25 行 `-- vunit: run_all_in_same_sim` 指令让所有用例同仿真跑；第 28 行 `runner_cfg : string` 是 VUnit 注入运行配置的固定入口。

DUT 实例化与自由运行的时钟（时钟写成 `Clk <= not Clk after 半周期`，无需进程）：

[test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd:75-106](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L75-L106) — DUT 实例化（把 TB 泛型透传给 DUT，如 `Depth_g => Depth_g`）与 100 MHz 时钟生成。

控制进程的「头—中—尾」三件套，这是**所有 Open Logic TB 都遵守的模板**：

[test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd:111-133](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L111-L133) — 第 111 行看门狗；第 115 行 `test_runner_setup(runner, runner_cfg)` 初始化；第 117 行 `while test_suite loop`；第 121–131 行每个用例共享的复位段（因为 `run_all_in_same_sim`，复位必须写在循环里）；第 133 行 `if run("Reset") then` 分发到第一个用例。

用例内部用 `check_equal(实际, 期望, "提示语")` 做断言，不匹配则记一次失败但不立即停止（VUnit 默认继续跑到 `cleanup` 再汇总）：

[test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd:133-147](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L133-L147) — `"Reset"` 用例：一串 `check_equal` 验证复位后各信号电平。注意第 141/144 行用 `if AlmFullOn_g then` 让同一用例**根据泛型自适应**检查项。

收尾——所有用例跑完后调用清理，VUnit 据此判定本仿真结束：

[test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd:408-412](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L408-L412) — `while test_suite` 循环结束、`test_runner_cleanup(runner)` 收尾。

> 小结口诀：**setup → while test_suite → 分发 if run → cleanup**，外加 watchdog 当保险。

#### 4.1.4 代码实践

**目标**：亲手跑一遍这个标准 TB，观察「同仿真跑全部用例」与「用例分发」。

**步骤**：

1. 进入 `sim/` 目录，用 GHDL 只跑 `olo_base_fifo_sync_tb`：
   ```bash
   cd sim
   python3 run.py --ghdl -v -k "*olo_base_fifo_sync*"
   ```
   （`-k` 只编译/运行名字匹配的 TB，`-v` 打开详细输出。命令与开关以你本地 `run.py --help` 为准。）
2. 观察输出里出现的一长串**具名用例**（如 `olo_tb.olo_base_fifo_sync_tb.<config>.Reset`、`...TwoWordsWriteAndRead`、`...WriteFullFifo` 等），它们是在同一次仿真里依次跑完的。
3. 故意制造一个失败：临时把 TB 第 135 行的期望 `'1'` 改成 `'0'`（**仅作练习，不要提交**），重跑，观察 VUnit 报告哪条 `check_equal` 失败、属于哪个用例，然后改回。

**需要观察的现象**：

- 用 `run_all_in_same_sim` 时，仿真器只 elaborate 一次，用例切换很快。
- 每个用例名都带有配置后缀（见 4.2），同一用例会因不同泛型组合跑很多遍。

**预期结果**：全部用例通过（绿）；改坏期望后该条 `check_equal` 变红，但其余用例仍继续跑完。

> 若本地无 GHDL：本步骤可改用 `--nvc`；若工具链不可用，则跳过执行、按上述「源码阅读型实践」理解即可，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 25 行 `-- vunit: run_all_in_same_sim` 删掉，行为会怎样变化？
**答案**：VUnit 会回到默认模式——**每个用例各启动一次独立仿真**。功能结果不变（用例仍逐个跑），但回归更慢，且每次仿真都要重新 elaborate。

**练习 2**：为什么复位段（第 121–131 行）必须写在 `while test_suite` 循环**内部**，而不是 `test_runner_setup` 之后只执行一次？
**答案**：因为 `run_all_in_same_sim` 让所有用例共享同一个仿真上下文，上一个用例可能把 DUT 留在任意状态。把复位放在循环内、每个用例开头都做一次，才能保证每个用例都从一个干净的复位态出发。

**练习 3**：`test_runner_watchdog(runner, 10 ms)` 解决什么问题？
**答案**：防止某个写错的激励（例如死等一个永不到来的 `Ready`）让仿真永远挂住、拖垮整个 CI 回归。超过 10 ms 未结束即强制失败。

---

### 4.2 泛型参数化用例

#### 4.2.1 概念说明

Open Logic 每个实体都有大量泛型（FIFO 的深度、RAM 的读写行为 RBW/WBR、复位期间 `Ready` 电平……）。逐一手写「每种泛型组合一个 TB 文件」既不现实也无法维护。VUnit 的解法是：**让 TB 自己携带这些泛型，再在 Python 侧用配置脚本把不同的泛型值组合注册成多个「具名配置」**。VUnit 会为每种不同的泛型集合各编译一份 TB，然后把每个用例 × 每个配置都跑一遍。

于是「一个 TB 文件」就能展开成几十甚至上百个仿真用例，覆盖各种参数组合——这正是 Open Logic「Trustable Code」承诺里高覆盖率的来源之一。

#### 4.2.2 核心流程

参数化分 VHDL 与 Python 两层协作：

```
[VHDL 层]                          [Python 层 sim/test_configs/<area>.py]
TB 实体声明泛型                     add_configs(olo_tb):
  Depth_g   : natural := 32          tb = olo_tb.test_bench("olo_base_fifo_sync_tb")
  RamBehavior_g : string := "RBW"    for RamBehav in ['RBW','WBR']:
  ReadyRstState_g : ...                  named_config(tb, {'RamBehavior_g': RamBehav})
  runner_cfg : string                for Depth in [32,128]:
                                         named_config(tb, {'Depth_g': Depth})
            │                             ...
            │   VUnit 读取配置，为每组不同泛型编译一份 TB
            ▼
   每个配置 → 每个用例(run) → 一个仿真实例
   全名形如：olo_tb.olo_base_fifo_sync_tb.Depth_g=128.TwoWordsWriteAndRead
```

`named_config(tb, map)`（[utils.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/utils.py)）做的事很轻：

- 把字典 `{'Depth_g': 128}` 拼成配置名 `"Depth_g=128"`；
- 调 VUnit 的 `tb.add_config(name=..., generics=map)`，VUnit 据此编译并运行。

关键认识：**配置名由泛型键值拼成，因此全名本身就说明了这次跑的是什么参数组合**，CI 日志里一眼就能定位是哪种配置失败。

#### 4.2.3 源码精读

VHDL 侧：`olo_base_fifo_sync_tb` 的泛型（[第 26–35 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_fifo_sync/olo_base_fifo_sync_tb.vhd#L26-L35)）就是参数化入口，`Depth_g`、`RamBehavior_g`、`ReadyRstState_g`、`AlmFullOn_g`、`AlmEmptyOn_g` 都有默认值，故「裸跑」（不带任何配置）也能跑出一个默认用例集。

Python 侧：`olo_base.py` 为这个 TB 注册了大量配置：

[sim/test_configs/olo_base.py:77-91](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L77-L91) — 为 `olo_base_fifo_sync_tb` 与 `olo_base_fifo_async_tb` 遍历 `RamBehavior_g`(RBW/WBR)、`ReadyRstState_g`(0/1)、`Depth_g`(32/128，同步 FIFO 还额外加一个奇数深度 53)、`AlmFullOn_g`×`AlmEmptyOn_g` 四种开关组合，每种调一次 `named_config`。

辅助函数实现：

[sim/test_configs/utils.py:15-21](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/utils.py#L15-L21) — `named_config`：把泛型字典拼成 `k=v-k=v` 形式的配置名，再委托 `tb.add_config(generics=map, pre_config=...)`。`pre_config` 钩子用于「仿真前先生成代码/数据文件」（见 4.3 与 u8-l5 的协仿真）。

> 维护要点：新增一个泛型组合，只需在对应 `test_configs/<area>.py` 里加一行 `named_config(...)`，VHDL 的 TB 一行都不用改。

#### 4.2.4 代码实践

**目标**：亲手为 `olo_base_fifo_sync_tb` 增加一组泛型配置，并确认它被发现并运行。

**步骤**：

1. 在 [olo_base.py 第 77–91 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L77-L91) 的 `fifo_tbs` 循环里，临时加一行测试一个新深度：
   ```python
   named_config(tb, {'Depth_g': 16, 'RamBehavior_g': 'WBR'})
   ```
2. 运行：
   ```bash
   cd sim
   python3 run.py --ghdl -v -k "*olo_base_fifo_sync*"
   ```
3. 在输出里找到配置名包含 `Depth_g=16-RamBehavior_g=WBR` 的用例，确认它们被编译并执行。

**需要观察的现象**：VUnit 列出的用例全名里出现了你新增的配置后缀；该配置下 `WriteFullFifo` 用例会按 `Depth_g=16` 填满 FIFO。

**预期结果**：新配置下所有用例通过。

> 若工具链不可用：阅读 `olo_base.py` 统计 `olo_base_fifo_sync_tb` 一共注册了多少组配置（粗略为 2×2×3×4，去重后约四十余组），即可理解为什么一个 TB 能撑起大量回归。标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 TB 实体的泛型要给**默认值**？
**答案**：默认值让 TB 在「不传任何泛型」时也能 elaborate 通过；同时 VUnit 配置只覆盖它关心的那几个泛型，其余沿用默认，配置脚本能保持简洁。

**练习 2**：配置名 `Depth_g=16-RamBehavior_g=WBR` 是怎么来的？去掉它对功能有影响吗？
**答案**：由 `named_config` 把字典 `{'Depth_g':16,'RamBehavior_g':'WBR'}` 用 `-` 拼接键值对生成。去掉这一行只是少跑一组参数组合，不影响其他配置的功能。

---

### 4.3 验证组件 VC

#### 4.3.1 概念说明

**验证组件（Verification Component, VC）**是一种**可复用的总线功能模型（BFM）**：它把某一种接口协议（AXI4、AXI-Lite、SPI、I2C、定点流……）的时序细节封装成一个黑盒，TB 不再手写逐拍的 `Valid`/`Ready` 电平，而是用**高层次的「事务」**（「写一个字」「读一段突发」「按文件逐拍比对输出」）来驱动或检查 DUT。

Open Logic 的 VC 建立在 VUnit 的**消息传递（com）API** 之上，核心三件套：

- **actor（信箱）**：每个 VC 实例内部有一个 `actor_t`，相当于一个消息队列。TB 往里投递事务请求，VC 内部进程取出请求并执行。
- **net（网络）**：所有 actor 共享的逻辑网络 `net : inout network_t`，是消息投递的通道。
- **procedure（事务接口）**：VC 的包提供 `push_*`（发起事务）和 `expect_*`（期望并检查响应）等过程，它们内部 `send(net, actor, msg)` 把事务打包成消息投出。

一个 VC 在代码里通常表现为**两段**：

1. **包（package）**：定义「实例类型」（一个含 `actor` 的 record）、构造函数 `new_olo_test_*`、各种 `push_*`/`expect_*` 事务过程、以及 `as_sync`（把实例转成同步句柄，供 `wait_until_idle` 用）。
2. **实体（entity）**：实例化用的硬件外壳，端口接 DUT 的引脚，内部进程 `receive(net, actor, msg)` 取消息、按协议驱动/采样引脚。

实例句柄是个 record 常量，在 TB 的 architecture 声明区用构造函数创建；实体则在并发区实例化、把句柄作为泛型传进去。多个 VC 可以同时存在（混用），最后用 `wait_until_idle(net, as_sync(每个VC))` 等它们都空闲再进入下一个用例。

#### 4.3.2 核心流程

以「AXI 主机 VC 驱动一次单拍写、再单拍读」为例：

```
[TB 控制进程]                         [VC 内部进程]
                                       receive(net, actor, msg)  ← 阻塞等消息
push_single_write(net, Master, addr, data)
   │  send(...) ──────────────────────▶ 收到 axi_aw_msg → 驱动 AW 通道握手
   │                                    收到 axi_w_msg  → 驱动 W 通道 + WLast
   │                                    收到 axi_b_msg  → 收 B 响应、check resp
expect_single_read(net, Master, addr, expData)
   │  send(...) ──────────────────────▶ 收到 axi_ar_msg → 驱动 AR 通道
   │                                    收到 axi_r_msg  → 收 R 数据、check_equal(data)
   ▼
wait_until_idle(net, as_sync(Master))  ◀── 等所有通道 initiated==completed
```

要点：

- **TB 与 VC 解耦**：TB 只描述「要做什么事务」，VC 负责把事务翻译成引脚时序。换 DUT 数据宽度时往往只改构造函数的 `data_width` 参数。
- **`expect_*` 自带检查**：例如 `expect_single_read` 会在收到读数据后自动 `check_equal`，相当于「驱动 + 断言」合一。
- **`wait_until_idle` 收尾**：事务是异步投递的（`send` 立即返回），TB 在用例末尾必须等 VC 把队列里的事务都做完，否则 `cleanup` 时可能还有事务没发完。

#### 4.3.3 源码精读

**AXI 主机 VC**——典型的「驱动 + 检查」型 VC。先看包：实例类型是一个 record（含 actor 和各通道宽度），构造函数创建 actor：

[test/tb/olo_test_axi_master_vc.vhd:29-35](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_axi_master_vc.vhd#L29-L35) — 实例类型 `olo_test_axi_master_t`：record 里第一个字段 `p_actor : actor_t` 就是消息信箱，其余字段缓存宽度供事务过程使用。

[test/tb/olo_test_axi_master_vc.vhd:147-156](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_axi_master_vc.vhd#L147-L156) — 构造函数 `new_olo_test_axi_master`（`new_actor` 建信箱）与 `as_sync`（把实例转成同步句柄，供 `wait_until_idle`）。

事务过程：底层是「投递消息」，例如单拍写 = AW + W + 期望 B 三条消息的组合：

[test/tb/olo_test_axi_master_vc.vhd:98-117](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_axi_master_vc.vhd#L98-L117) — `push_single_write` 与 `expect_single_read`：分别组合 `push_aw/push_w/expect_b` 和 `push_ar/expect_r`，对外提供「一次完整事务」的高层接口。底层每个 `push_*` 都是 `new_msg` → `push(字段)` → `send(net, actor, msg)`（见 [push_aw 第 162–185 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_axi_master_vc.vhd#L162-L185)）。

实体外壳与内部驱动：实体端口直接接 AXI 主从信号，内部主进程 `receive` 取消息后分发到 AW/AR/W/B/R 五个子进程，每个子进程按协议握手：

[test/tb/olo_test_axi_master_vc.vhd:415-424](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_axi_master_vc.vhd#L415-L424) — 实体声明：泛型 `instance` 接收句柄，端口 `axi_ms`/`axi_sm` 是主/从信号组（record 类型）。

[test/tb/olo_test_axi_master_vc.vhd:463-505](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_axi_master_vc.vhd#L463-L505) — 主进程 `receive(net, instance.p_actor, request_msg)` 取消息，按 `msg_type` 分发到对应通道队列；第 489–500 行处理 `wait_until_idle_msg`：等所有通道的「已发起数 == 已完成数」。

[test/tb/olo_test_axi_master_vc.vhd:557-564](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_axi_master_vc.vhd#L557-L564) — AW 子进程把事务翻译成引脚时序：拉高 `aw_valid`、`wait until rising_edge(clk) and aw_ready='1'` 完成一次握手。

**定点检查器 VC**——典型的「纯检查」型 VC：不驱动 DUT，而是读一个期望文件，逐拍与 DUT 输出 `check_equal`。包结构与 AXI VC 同构：

[test/tb/olo_test_fix_checker_vc.vhd:25-58](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_fix_checker_vc.vhd#L25-L58) — 实例类型（含 `p_actor`）、唯一事务过程 `fix_checker_check_file`（指定期望文件、模式 stream/packet/tdm、随机停顿参数）、构造函数与 `as_sync`。

实体内部：读文件、按 AXI-S 握手推进、逐拍比对：

[test/tb/olo_test_fix_checker_vc.vhd:284-292](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_fix_checker_vc.vhd#L284-L292) — 在握手成功的那一拍 `check_equal(data, data_slv, ...)` 比对，错误信息带「文件名 + 行号」，定位极快。第 268–287 行处理 `is_timing_master`：作为 timing master 时 VC 自己掌控 `ready`（可注入随机停顿），作为 slave 时只被动等 `ready`。

**TB 工具包**——不是 VC，而是 TB 里最常用的过程集合，常与 VC 配合做「无活动检查」「等待某值」「脉冲」：

[test/tb/olo_test_activity_pkg.vhd:72-79](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_activity_pkg.vhd#L72-L79) — `check_no_activity_stdl`：等待 `idle_time` 后用 `sig'last_event >= idle_time` 断言该信号这段时间没有翻转，用来验证「空闲时确实静默」（如复位后总线无毛刺）。

[test/tb/olo_test_activity_pkg.vhd:117-132](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_activity_pkg.vhd#L117-L132) — `wait_for_value_stdlv`：在 `timeout` 内等信号到达期望值，超时则 `error`，避免 TB 死等。

#### 4.3.4 代码实践

**目标**：阅读两个「VC 实战范本」，看清楚 VC 在 TB 里如何声明、驱动、检查、收尾。

**步骤**：

1. 打开 [olo_axi_lite_slave_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd)。
   - 声明区创建句柄：[第 80–85 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L80-L85) `AxiMaster_c := new_olo_test_axi_master(...)`。
   - 用例里驱动并检查：[第 135 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L135) `push_single_write` 写一个寄存器、随后在 Rb 侧 `check_equal`；[第 156 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L156) `expect_single_read` 读回。
   - 收尾：[第 278 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L278) `wait_until_idle(net, as_sync(AxiMaster_c))`。
   - 实例化外壳：[第 339–347 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L339-L347) `vc_master : entity work.olo_test_axi_lite_master_vc`，把句柄当泛型传进去。
2. 打开 [olo_fix_round_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_round/olo_fix_round_tb.vhd)，对比「双 VC 混用」：
   - [第 68–70 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_round/olo_fix_round_tb.vhd#L68-L70) 同时创建 `Stimuli_c`（激励）与 `Checker_c`（检查）两个句柄。
   - [第 99–100 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_round/olo_fix_round_tb.vhd#L99-L100) 一个回放激励文件、一个比对期望文件。
   - [第 110–111 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_round/olo_fix_round_tb.vhd#L110-L111) 分别 `wait_until_idle` 两个 VC。
   - [第 160–181 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_round/olo_fix_round_tb.vhd#L160-L181) 实例化两个 VC 外壳。

**需要观察的现象**：

- TB 控制进程里**没有任何 AXI/定点的逐拍时序代码**，全是高层事务调用——这就是 VC 的价值。
- 混用多个 VC 时，每个 VC 各自 `wait_until_idle`，互不干扰。

**预期结果**：能口述出「句柄在声明区 new、事务在进程里 push/expect、收尾 wait_until_idle、外壳在并发区实例化」这四步。

> 说明：定点 VC 依赖 `.fix` 期望文件，这些文件由 VUnit 的 `pre_config` 钩子在仿真前用 Python 位真模型生成（见 u8-l5、u10-l2）。本实践只读 TB 结构，不涉及文件生成。

#### 4.3.5 小练习与答案

**练习 1**：为什么事务过程（`push_single_write`）用 `send` 投递消息后**立即返回**，而不等 AXI 握手真的完成？TB 又如何确保事务都做完了？
**答案**：消息投递是异步的，`send` 只把请求放进 VC 的信箱就返回，让 TB 能连续「下命令」而不被慢握手阻塞。由于异步，TB 在用例末尾必须 `wait_until_idle(net, as_sync(VC))`，等 VC 把队列里所有事务都执行完，再进入下一步或 `cleanup`。

**练习 2**：`expect_single_read` 既是「驱动」也是「检查」，它检查的是什么？
**答案**：它驱动 AR 通道发起读，并在收到 R 通道数据后自动 `check_equal(r_data, 期望, ...)`、检查 `r_resp`。因此一次调用同时完成「发起读」和「校验读回数据」。

**练习 3**：定点 checker VC 既可作 timing master 也可作 slave，二者区别是什么？
**答案**：作 master 时 VC 自己驱动 `ready`（并可注入随机停顿 `stall_probability` 来施压 DUT 的反压逻辑）；作 slave 时 `ready` 由外部（另一个 master VC）掌控，checker 只被动等待握手。多路同拍配对时，每个方向恰好一个 master（见 u8-l5 的 timing-master/slave 约定）。

---

### 4.4 VC 命名约定

#### 4.4.1 概念说明

Open Logic 的生产代码有一套严格的命名规范（后缀 `_g/_c/_t`、`olo_<area>_<function>`、PascalCase 风格的标识符如 `In_Data`，见 u1-l5）。但 **VC 是例外**：它遵循 **VUnit 自身的命名风格——`snail_case`（全小写 + 下划线）**，且**没有强制的后缀/前缀**。

为什么要搞两套？因为 Open Logic 的 VC 要能和 VUnit **官方自带的 VC**（如 `axi_stream`、`memory` 等）无缝混用，统一成 VUnit 风格才能保持 TB 代码的一致性。而 VHDL 本身**大小写不敏感**，所以「VC 用 snail_case 声明、TB 用 Open-Logic 风格实例化」在语法上完全等价，又让两边的 linter（VSG）各得其所、互不报错。

#### 4.4.2 核心流程

约定包含四条：

```
1. 位置：所有 VC 放在 <root>/test/tb/，统一以 olo_test_*_vc 命名（如 olo_test_axi_master_vc）。
2. 标识符：VC 内部全部 snail_case（some_generic / some_port），无强制后缀。
3. 实例化：在 TB（Open-Logic 风格）里实例化 VC 时，用 PascalCase 风格（Some_Generic => ...）。
4. 可混用：Open Logic VC 与 VUnit 原生 VC 风格一致，可在同一个 TB 里并用。
```

#### 4.4.3 源码精读

约定原文（含示意代码）：

[doc/Conventions.md:70-100](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md#L70-L100) — 第 72–73 行：VC 放 `test/tb`，遵循 VUnit 命名约定以便与原生 VUnit VC 混用；第 75 行：VC 标识符全部 `snail_case`、无强制后缀前缀；第 77–79 行：VC 与生产代码的差异**仅在于标识符大小写**；第 81–100 行用代码示例说明「VC 用 snail_case 声明、TB 用 Open-Logic 风格实例化」，并指出这得益于 VHDL 大小写不敏感。

真实对照（已在 4.3 读过的 AXI 主机 VC）：VC 实体声明里端口是 `clk`、`axi_ms`（snail_case）：

[test/tb/olo_test_axi_master_vc.vhd:415-424](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_axi_master_vc.vhd#L415-L424) — VC 端口用 `clk`/`axi_ms`/`axi_sm`（snail_case，VUnit 风格）。

而 TB 实例化它时用 `Clk`/`Axi_Ms`（Open-Logic 风格）：

[test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd:339-347](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_lite_slave/olo_axi_lite_slave_tb.vhd#L339-L347) — `vc_master : entity work.olo_test_axi_lite_master_vc`，泛型与端口名都写成 `Instance`/`Clk`/`Axi_Ms`（Open-Logic 风格）。两种大小写在 VHDL 里指向同一标识符，但各自的 linter 都满意。

VC 目录全貌（`test/tb/` 下都是 `olo_test_*_vc` 或 `olo_test_*_pkg`）：

```text
test/tb/
├── olo_test_activity_pkg.vhd        ← TB 工具包（非 VC，但同属可复用测试代码）
├── olo_test_pkg_axi.vhd             ← AXI record 类型定义（axi_ms_t / axi_sm_t）
├── olo_test_axi_master_vc.vhd       ← AXI4 / AXI-Lite 主机 VC
├── olo_test_axi_slave_vc.vhd        ← AXI4 从机 VC
├── olo_test_fix_stimuli_vc.vhd      ← 定点激励 VC（回放 .fix）
├── olo_test_fix_checker_vc.vhd      ← 定点检查 VC（比对 .fix）
├── olo_test_spi_master_vc.vhd       ← SPI 主机 VC
├── olo_test_spi_slave_vc.vhd        ← SPI 从机 VC
└── olo_test_i2c_vc.vhd              ← I2C VC
```

> 注意：`olo_test_pkg_axi.vhd` 定义了 VC 与 TB 共用的 `axi_ms_t`/`axi_sm_t` record（把 AXI 五通道信号打包），这是 AXI VC 能用 record 端口的前提。

#### 4.4.4 代码实践

**目标**：用 linter 验证「两套命名共存」的约定真的成立。

**步骤**：

1. 在仓库根目录运行 VSG 检查（lint 工具用法见 u10-l4）：
   ```bash
   # 具体命令以 lint/script/script.py 为准（见 u10-l4）
   python3 lint/script/script.py
   ```
2. 观察：VC 文件（`test/tb/olo_test_*_vc.vhd`）按 snail_case 规则检查，TB 文件按 Open-Logic 规则检查，两者**都不报错**。
3. 阅读 [Conventions.md 第 81–100 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md#L81-L100) 的示意代码，对照真实文件确认「声明 snail_case、实例化 PascalCase」。

**需要观察的现象**：lint 对生产代码、TB、VC 三类文件用不同规则集，全部通过。

**预期结果**：无 error/warning（PR 必须 lint 清零，见 u1-l5）。

> 若本地未配 VSG：阅读 `lint/config/` 下的 VSG 配置（见 u10-l4），理解它如何区分 VC 与生产代码的规则。标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 VC 用 snail_case，而生产代码用 Open-Logic 风格？两套规则为什么不打架？
**答案**：为了与 VUnit 原生 VC 风格一致、便于混用。不打架是因为 VHDL 标识符**大小写不敏感**，`clk` 与 `Clk` 是同一个标识符；声明侧和实例化侧用不同的大小写只是「书写风格」，linter 对不同文件套用不同规则集，各自都满足。

**练习 2**：VC 的实体名为什么是 `olo_test_axi_master_vc` 而不是 `olo_base_axi_master`？
**答案**：`olo_test_*_vc` 前缀表明它是**测试专用**的验证组件（不可综合、不进生产库），放在 `test/tb/`，与生产实体（`olo_<area>_*`，在 `src/`）明确区分；这也让 linter 能凭名字/路径识别并套用 VC 规则。

---

## 5. 综合实践

把四个模块串起来：**仿写一个带两个泛型参数化用例、并用一个 VC 做检查的 VUnit 测试台**。

**任务**：为简单实体 `olo_fix_resize`（定点格式转换，单入单出 AXI-S 流，详见 u8-l3）写一个最小 TB，结构完全照搬 `olo_fix_round_tb`：

1. **TB 骨架**（对应 4.1）：
   - 实体泛型：`AFmt_g`、`ResultFmt_g`、`Round_g`、`Saturate_g` 以及固定的 `runner_cfg : string`；实体上方写 `-- vunit: run_all_in_same_sim`。
   - 控制进程：`test_runner_watchdog` → `test_runner_setup` → `while test_suite` → 至少两个 `run("...")` 分支（如 `"FullSpeed"` 与 `"Throttled"`）→ `test_runner_cleanup`。
2. **用 VC 做检查**（对应 4.3）：
   - 声明 `Stimuli_c : olo_test_fix_stimuli_t := new_olo_test_fix_stimuli;` 与 `Checker_c : olo_test_fix_checker_t := new_olo_test_fix_checker;`。
   - 在用例里 `fix_stimuli_play_file(net, Stimuli_c, AFile_c)` 驱动、`fix_checker_check_file(net, Checker_c, ResultFile_c)` 比对输出；末尾对两个 VC 各 `wait_until_idle(net, as_sync(...))`。
   - 并发区实例化 `olo_test_fix_stimuli_vc` 与 `olo_test_fix_checker_vc`（注意 4.4：实例化用 Open-Logic 风格 `Instance`/`Fmt`/`Clk`）。
3. **两个泛型参数化用例**（对应 4.2）：在 `sim/test_configs/olo_fix.py`（仿照 `olo_base.py` 风格）为你的 TB 加两组配置，例如：
   ```python
   named_config(tb, {'AFmt_g': '(1,8,8)', 'ResultFmt_g': '(1,8,4)', 'Round_g': 'Trunc_s'})
   named_config(tb, {'AFmt_g': '(1,8,8)', 'ResultFmt_g': '(1,8,4)', 'Round_g': 'NonSymPos_s'})
   ```
4. **期望文件来源**：定点 VC 依赖 `.fix` 文件，需用 `pre_config` 钩子由 Python 位真模型生成（机制见 u8-l5 / u10-l2）。若暂未接协仿真，可先把 TB 写好、用「源码阅读型」方式核对结构，标注「待本地验证」。

**验收**：

- VHDL 侧能说清 setup/while-test_suite/run/check_equal/cleanup 五件套；
- Python 侧能说清 `named_config` 如何把泛型字典变成具名配置；
- 能指出 TB 里「声明 snail_case、实例化 PascalCase」的两处对照；
- （可选）`run.py -k "*<你的TB>*"` 能发现并跑通新增的两个配置。

---

## 6. 本讲小结

- **标准 TB 骨架** = `runner_cfg` 泛型 + `test_runner_setup` + `while test_suite` + `if run("X")` 分发 + `check_equal` 断言 + `test_runner_cleanup`，外加 `watchdog` 防挂死；`-- vunit: run_all_in_same_sim` 让同 TB 的所有用例在同一次仿真里跑完（快，但每用例须自行复位）。
- **泛型参数化**：TB 带泛型、Python `test_configs` 用 `named_config` → `tb.add_config` 注册多组泛型组合，一组配置 × 每个用例 = 一个仿真实例，配置名（`k=v-k=v`）自带可读性。
- **验证组件 VC** 是建立在 VUnit 消息机制（actor/net/`push_*`/`expect_*`）上的可复用 BFM：声明区 `new_*` 建句柄、进程里下事务、`wait_until_idle` 收尾、并发区实例化外壳；`expect_*` 自带检查，多个 VC 可混用。
- **VC 命名约定**：放 `test/tb/`、命名 `olo_test_*_vc`、内部 `snail_case`、在 TB 里以 Open-Logic 风格实例化（VHDL 大小写不敏感，故两套规则共存而不冲突），便于与 VUnit 原生 VC 混用。
- **TB 工具包** `olo_test_activity_pkg`（`check_no_activity`、`wait_for_value`、`pulse_sig`）是 VC 之外最常用的可复用辅助，常用于空闲检查与超时等待。

---

## 7. 下一步学习建议

- **u10-l2（仿真运行器、代码生成与测试配置）**：深入 `sim/run.py` 的 VUnit 集成与 `--coverage`/`--compile_list` 选项，以及 `pre_config` 钩子如何在仿真前生成 `.fix` / 定点包文件——这是本讲 VC 实践里「期望文件从哪来」的答案。
- **u10-l3（覆盖率、问题分析、徽章）**：理解 TB 与 VC 写出来后，如何被收集成覆盖率、如何在 PR 到 main 时被 95% 阈值门禁拦截。
- **u8-l5（协仿真）**：若要做综合实践里的定点 VC 端到端流程，这是必读的「Python 位真模型 + 文件中介 + HDL 逐拍比对」完整链路。
- **源码延伸阅读**：`test/tb/olo_test_axi_slave_vc.vhd`（AXI 从机 VC，可与主机 VC 对接做回环）、`test/tb/olo_test_spi_master_vc.vhd` 与 `olo_test_i2c_vc.vhd`（外部接口 VC，体会不同协议下 VC 的同构性）。
