# u8-l2 VUnit 仿真框架与 cosim_runner 单例调度

## 1. 本讲目标

本讲是验证单元（单元 8）的第二课，承接 u8-l1（cosim 的 Python 一侧：如何生成黄金参考文件）。学完本讲后，你应该能够：

- 说清 `sim/run.py` 是如何用 **VUnit** 这个开源 VHDL 验证框架，把 RTL 源码、testbench、en_tb 库组织成可运行的 test suite 的。
- 解释 `pre_config` 回调钩子是怎么把「VHDL 仿真」与「Python 生成黄金参考」这两件事缝合到一起的。
- 理解 `sim/cosim_runner.py` 这个**线程安全的单例**为什么能保证每个 cosim 脚本在一整轮仿真里**最多只跑一次**，即使 VUnit 并发执行多个 test。
- 读懂 `sim/common.py` 对 GHDL / NVC / Modelsim / Questa 四种仿真器的差异配置（尤其是 VHDL 标准和编译选项）。
- 描述一次仿真从 `pre_config` 触发 cosim、到 testbench 读文件对拍、再到 `post_run` 收尾的完整时序。

## 2. 前置知识

本讲是 **advanced** 级别，需要你已经掌握：

- **u8-l1** 的核心结论：cosim = Python 参考模型算黄金参考 → 写盘 → VHDL testbench 逐位对拍；`data/` 目录下的文件约定（`test{N}_output.txt`、`a_fmt.txt`、`rnd.txt`、`sat.txt`）。
- **u1-l3** 的仓库结构：`hdl/`（RTL）、`tb/`（testbench）、`sim/`（验证驾驶舱）、`lib/en_tb/`（通用 testbench 库）。
- **u2-l3** 的 VHDL 包镜像：RTL 遵循 VHDL-93，testbench 遵循 VHDL-2008。

下面几个术语本讲会反复用到，先做个最小解释：

- **VUnit**：一个用 Python 编写、面向 VHDL/Verilog 的开源验证框架。它帮你管理「编译哪些源文件、用哪个仿真器、跑哪些 test、怎么并行」，其核心对象是 `VUnit`（代表一次验证工程）和 **test suite**（一组 testbench 的集合）。en_cl_fix 通过 pip 依赖它（见 `requirements.txt` 中的 `vunit-hdl`）。
- **testbench（TB）**：只用于仿真、不能综合成电路的 VHDL 代码，负责给 RTL 喂激励、检查输出。本仓库里 `tb/*.vhd` 都是 TB。
- **pre_config / post_run 回调**：VUnit 提供的两个「钩子」。`pre_config` 在**某个 test 的仿真开始之前**被调用，常用来「在跑仿真前先准备数据」；`post_run` 在**全部仿真结束之后**被调用一次，常用来「合并覆盖率」。本仓库正是用 `pre_config` 触发 cosim 脚本。
- **黄金参考（golden reference）**：u8-l1 已讲，指 Python 模型算出的「标准答案」，VHDL TB 的输出要和它逐位相等。

## 3. 本讲源码地图

本讲只精读 `sim/` 目录下的三个 Python 文件，它们构成验证流程的「控制层」（真正算数据的 cosim 脚本是 u8-l1 的内容，真正对拍的 TB 是 u8-l3 的内容）。

| 文件 | 作用 | 行数 |
|------|------|------|
| `sim/common.py` | 验证脚手架的「地基」：加载 VUnit、定义命令行参数、选定仿真器、设置 VHDL 标准、提供 `post_run` 回调。 | 108 行 |
| `sim/run.py` | 验证驾驶舱入口：创建 VUnit 工程、组织 test suite（注册库、源文件、TB），把每个 TB 的 `pre_config` 钩子绑到对应的 cosim 脚本。 | 253 行 |
| `sim/cosim_runner.py` | 一个**线程安全、只执行一次**的 cosim 运行器：负责把同名 `cosim.py` 模块安全地导入，并在 `pre_config` 触发时最多运行它的 `run()` 一次。 | 73 行 |

三者关系一句话概括：`common.py` 搭台 → `run.py` 唱戏（组织 test suite）→ `cosim_runner.py` 负责「黄金参考只生成一次」的并发安全保证。

## 4. 核心概念与源码讲解

### 4.1 common.py：搭好 VUnit 的运行地基

#### 4.1.1 概念说明

`common.py` 解决的是一个朴素问题：**「我要用哪个仿真器、从哪里调用它、按哪个 VHDL 标准编译？」** 它把答案做成模块级常量，供 `run.py` 直接取用。

它的设计有几个值得注意的点：

1. **VUnit 不是写死在仓库里的**，而是通过 Python 的 `import` 机制加载。仓库预期 vunit 可能被「vendor 预置」在某个目录，也可能直接走 pip 安装的包。
2. **仿真器差异被集中收口**：四种仿真器（GHDL、NVC、Modelsim、Questa）在 VHDL 标准支持、编译/仿真选项上各不相同，`common.py` 用一组 `if` 把它们归一成两个变量：`vhdl_standard_rtl` 和 `vhdl_standard_tb`。
3. **它 import 时即「执行」**：`common.py` 在被 import 的瞬间就会解析命令行、校验参数、写环境变量。这意味着 `run.py` 只要 `import common`，地基就铺好了。

#### 4.1.2 核心流程

`common.py` 的执行流程（import 时发生）：

```text
1. 把（可能存在的）vendor vunit 目录加到 sys.path，再 import VUnit/VUnitCLI
2. 构造 VUnitCLI，扩展 5 个自定义命令行参数：
     --simulator / --simulator-path / --vendor-lib / --coverage / --disable-cosim
3. parse_args() 解析命令行
4. 校验：simulator 与 simulator-path 必填，否则抛异常
5. 写 VUnit 专用的环境变量（VUNIT_SIMULATOR、各仿真器 *_PATH）
6. 按 simulator 选定 vhdl_standard_rtl 与 vhdl_standard_tb 两个常量
7. 定义 post_run(results) 回调（覆盖率合并），交给 run.py 传给 vu.main()
```

注意第 1 步：`sys.path.insert` 加的目录在本仓库里**并不存在**（`git ls-files` 确认 `lib/FW/...` 没有被跟踪），所以那行实际上是「有则用之、无则无害」的占位，真正的 `vunit` 模块最终由 `requirements.txt` 里的 `vunit-hdl==5.0.0.dev6` 提供。

#### 4.1.3 源码精读

**加载 VUnit 并扩展命令行参数。** VUnit 自带一个 `VUnitCLI`，这里给它**追加了 5 个本工程专属的参数**。其中 `--simulator` 与 `--simulator-path` 优先读环境变量 `EN_SIM_NAME` / `EN_SIM_BIN`，方便 CI 里复用：

[sim/common.py:24-35](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/common.py#L24-L35) — 先把（可能存在的）vendor vunit 目录插到 `sys.path`，再 `from vunit import VUnitCLI, VUnit`；接着注册 `--simulator` 参数。

其余四个参数（`--simulator-path`、`--vendor-lib`、`-c/--coverage`、`--disable-cosim`）结构与上面一致：

[sim/common.py:36-59](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/common.py#L36-L59) — 注意 `--disable-cosim`（`store_true`，默认 `False`），它会被传给 `cosim_runner`，决定是否真正跑 cosim 脚本。

**参数校验与环境变量写入。** `simulator` 与 `simulator-path` 缺一不可，否则直接抛异常给出友好提示；随后把仿真器名和路径写进 VUnit 约定的环境变量：

[sim/common.py:62-79](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/common.py#L62-L79) — Questa 有个小技巧：VUnit 暂不认 `questa` 这个名字，于是把 `VUNIT_SIMULATOR` 仍写成 `modelsim`，但路径指向 Questa 安装目录。

**按仿真器选定 VHDL 标准**——这是 `common.py` 最关键的一段。Modelsim/Questa 对 VHDL-93 支持完整，故 RTL 用 `93`、TB 用 `2008`；而 GHDL/NVC 更现代，RTL 与 TB 统一用 `2008`：

[sim/common.py:81-89](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/common.py#L81-L89) — 由此 `vhdl_standard_rtl` 在 modelsim/questa 下是 `"93"`，在 ghdl/nvc 下是 `"2008"`；`vhdl_standard_tb` 恒为 `"2008"`。这两个常量会被 `run.py` 在添加源文件时逐文件传入。

**post_run 回调**——全部仿真跑完后再合并覆盖率（仅 modelsim/questa 支持；nvc 给告警；ghdl 无覆盖）：

[sim/common.py:92-108](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/common.py#L92-L108) — 它把覆盖率数据合并到 `.ucdb` 并调用 `vsim -c -viewcov` 生成报告。

#### 4.1.4 代码实践

**实践目标**：亲手感受 `common.py` 的「import 即执行」特性，并验证 VHDL 标准的选择逻辑。

**操作步骤**（在仓库根目录）：

1. 先故意不带参数运行，看 `common.py` 的校验是否拦住你：

   ```bash
   python sim/run.py
   ```

2. 再带 `--simulator=ghdl` 但**不带** `--simulator-path`，观察第二道校验。

3. （可选）写一段三行的「示例代码」，import `common` 的逻辑来验证标准选择（注意：直接 `import common` 会触发完整命令行解析并报错，因此这里只是**示意**，不建议真的运行）：

   ```python
   # 示例代码：演示 common.py 中 VHDL 标准的选择逻辑（不依赖 import common）
   def vhdl_standard_for(simulator):
       if simulator in ("modelsim", "questa"):
           return "93", "2008"      # rtl, tb
       elif simulator in ("ghdl", "nvc"):
           return "2008", "2008"
       raise ValueError(f"unknown simulator {simulator}")
   ```

**需要观察的现象**：第 1 步应打印 `ERROR: please use --simulator <name> ...`；第 2 步应打印 `ERROR: please use --simulator-path <path> ...`。

**预期结果**：两条错误提示分别来自 `common.py` 第 66 行与第 68 行。若你的环境里没有装 GHDL 等仿真器，本实践到此为止即可——这两条错误恰好证明了 `common.py` 在 import 阶段就完成了参数校验。

> 待本地验证：第 1、2 步的实际报错文案与退出码请在你本机确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Questa 要把 `VUNIT_SIMULATOR` 设成 `modelsim`？
**答案**：因为 `common.py` 第 72 行注释说明——VUnit 当时尚不接受 `questa` 作为合法仿真器名，于是复用 `modelsim` 通路，只把安装路径指向 Questa 目录。

**练习 2**：用 GHDL 跑仿真时，RTL 文件会按哪个 VHDL 标准编译？
**答案**：`2008`（见 `common.py:85-87`）。只有 Modelsim/Questa 才把 RTL 编成 `93`。

---

### 4.2 run.py：用 VUnit 组织 testbench，并用 pre_config 钩住 cosim

#### 4.2.1 概念说明

`run.py` 是整个验证流程的**总调度**。它要回答三个问题：

1. **编译什么**：哪些源文件进 `en_tb` 库、哪些进 `lib` 库、各自用哪个 VHDL 标准。
2. **跑什么**：仓库里有 11 个 TB（`cl_fix_add_tb`、`cl_fix_round_tb`……），怎么把它们注册进 VUnit 的 test suite。
3. **怎么和 cosim 联动**：每个 TB 仿真之前，必须先让对应的 cosim 脚本把 `data/` 目录的黄金参考文件生成好。这正是 `pre_config` 钩子的用武之地。

`run.py` 的精妙之处在于它把 cosim 的「调用哪个脚本」做成了**一个可特化的工厂**：先定义一个通用 `cosim_runner`（4.3 节详讲），再用闭包/子类把「目录名」钉死，得到一个针对单个运算（如 `cl_fix_add`）的专用运行器。

#### 4.2.2 核心流程

`run.py` 的执行流程：

```text
__main__:
  args = common.args                      # 取 common 已解析好的参数
  vu = VUnit.from_args(args)               # 用命令行参数创建 VUnit 工程
  vu.add_vhdl_builtins()                   # 加入 VHDL 内建库
  create_test_suite(vu, args)              # 见下
  vu.main(post_run=common.post_run)        # 交给 VUnit 驱动编译+仿真

create_test_suite(vu, args):
  1. 加入 VUnit 自带库（osvvm / verification_components / random）
  2. 加入 en_tb 库（VHDL-2008），用 try/except 容忍重复创建
  3. 创建 lib 库，依次加入：
       - ../hdl/*.vhd         （RTL，用 vhdl_standard_rtl）
       - ../tb/util/*.vhd     （en_cl_fix 对 en_tb 的扩展，TB 标准）
       - ../tb/*.vhd          （所有 testbench，TB 标准）
  4. 对每个 TB：
       - 例化一个专用 cosim 运行器（指向对应的 cosim 子目录）
       - 用 test.add_config(pre_config=...) 把它的 run 绑成 pre_config 回调
  5. 设置各仿真器的编译/仿真选项
```

第 4 步是本讲的核心：`pre_config` 把「Python 写数据」和「VHDL 读数据」串成因果链——**没有 pre_config 生成的文件，TB 就无文件可读**。

#### 4.2.3 源码精读

**入口 `__main__`**：三行就把控制权交给 VUnit，`post_run` 传的是 `common.post_run`（覆盖率合并）：

[sim/run.py:239-252](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L239-L252) — 注意 `args = common.args`，它直接复用 `common.py` 在 import 时已解析好的参数对象。

**库与源文件的组织**（test suite 的「骨架」）。`en_tb` 库的加入用 `try/except ValueError` 容忍「已被别的机制创建过」的情况；`lib` 库则把 RTL（`hdl/`）和 TB（`tb/`、`tb/util/`）按各自 VHDL 标准分别编译：

[sim/run.py:31-50](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L31-L50) — RTL 用 `vhdl_standard_rtl`（GHDL/NVC 下为 2008，Modelsim/Questa 下为 93），TB 与 util 用 `vhdl_standard_tb`（恒为 2008）。这正是 u2-l3 讲过的「RTL-93 / TB-2008」区分的落地。

**cosim 运行器的「特化工厂」**。这里定义一个内部类 `cosim`，继承自 `cosim_runner`，把 `COSIM_PATH` 与子目录名拼好：

[sim/run.py:56-63](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L56-L63) — `super().__init__(args.disable_cosim, cosim_subdir)` 把「是否禁用 cosim」和「脚本路径」一起传给基类。

**最朴素的 TB 注册模式**（以 `cl_fix_add` 为代表，共 8 个 TB 用同一套写法）：取该 TB 下所有名为 `test` 的测试，给每个都加一个配置，`pre_config` 指向同一个 `cl_fix_add_cosim.run`：

[sim/run.py:65-74](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L65-L74) — 这里 `cl_fix_add_cosim` 是一个**共享实例**，多个 test 配置都指向它的 `.run` 方法，于是 cosim 只会在首个配置触发时真正执行一次（4.3 节解释原因）。

**带 generic 展开的注册模式**（`cl_fix_round`/`saturate`/`resize` 三个 RTL 实体 TB）：这三个 TB 要验证 `meta_width_g` 这个 generic 取 0 和 8 两种值，于是取**同一个 test**、给它加**两个 config**（`MetaWidth=0` 与 `MetaWidth=8`），两个 config 仍共享同一个 cosim 运行器：

[sim/run.py:153-165](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L153-L165) — 这种「一个 test 多个 config」的写法是 VUnit 的标准用法，用来在**同一份 TB 代码**下跑多组 generic。

**各仿真器编译/仿真选项**。GHDL 用 `-frelaxed` 宽容处理、NVC 用 `--check-synthesis` 顺便做综合检查、Modelsim/Questa 开覆盖率，最后对所有仿真器统一关掉 IEEE warnings：

[sim/run.py:210-231](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L210-L231) — 这些选项与 `common.py` 的仿真器选择配套，是「跨四仿真器都能编译通过」的工程补丁。

#### 4.2.4 代码实践

**实践目标**：用 VUnit 的「列测试」功能，在不实际仿真、不实际跑 cosim 的情况下，看清 `run.py` 到底注册了多少个 test 配置。

**操作步骤**：

1. 确认 `vunit-hdl` 已装：`pip install -r requirements.txt`。
2. 用 VUnit 的 `-l`（list）参数列出所有 test（这一步会执行编译前的 test suite 构建，但通常不跑 cosim、不仿真）：

   ```bash
   python sim/run.py --simulator=ghdl --simulator-path $(which ghdl | xargs dirname) -l
   ```

   > 待本地验证：`-l` 是否触发 cosim 取决于 VUnit 版本；若担心副作用，可加 `--disable-cosim`。

3. 在输出里数一下 `cl_fix_round_tb` 相关的条目：应当能看到 `...MetaWidth=0` 和 `...MetaWidth=8` 两个 config，对应 [sim/run.py:159-165](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L159-L165) 的循环。

**需要观察的现象**：列表里每个运算 TB（add/sub/mult/...）各有一组 `test` 条目；round/saturate/resize 各有两条（meta_width=0/8）。

**预期结果**：共 11 个运算 TB 被注册；其中 3 个（round/saturate/resize）各展开为 2 个 config。

**源码阅读型实践（无仿真器时）**：不运行命令，直接对照 [sim/run.py:65-204](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L65-L204) 手工列出全部 11 个 `cosim(...)` 例化点，并标注哪些用了 meta_width 循环、哪些没用。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `en_tb` 库的加入要包在 `try/except ValueError` 里？
**答案**：因为 VUnit 里库名必须唯一；某些集成场景下 `en_tb` 可能已被预创建，重复 `add_library` 会抛 `ValueError`，这里捕获后打印「already created, skip it」优雅跳过（见 [run.py:37-41](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L37-L41)）。

**练习 2**：`cl_fix_round_tb` 的两个 config（MetaWidth=0/8）共享同一个 `cl_fix_round_cosim.run`，会不会导致 cosim 跑两遍？
**答案**：不会。`run` 内部有自锁与自禁用机制（4.3 节），首跑后即把自己关掉，第二次调用直接跳过。

---

### 4.3 cosim_runner.py：线程安全的「只跑一次」单例

#### 4.3.1 概念说明

`cosim_runner.py` 解决的是一个**并发安全**问题。

VUnit 默认会**并行**跑多个 test（每个 test 在一个线程里）。设想 `cl_fix_add_tb` 注册了若干 test 配置，它们的 `pre_config` 都指向同一个 `cl_fix_add_cosim.run`。如果没有任何保护，那么并行启动时，**多个线程会同时触发 cosim 脚本**，争着写同一个 `data/` 目录，结果会是灾难性的——文件互相覆盖、内容错乱。

`cosim_runner` 用「**双重检查锁 + 自禁用**」保证一个不变式：

\[
\text{每个 cosim\_runner 实例在一整轮仿真里，其 run() 的实际执行次数} \leq 1
\]

不仅如此，它还要解决第二个麻烦：所有运算的 cosim 脚本**都叫 `cosim.py`**（分别在 `cl_fix_add/cosim.py`、`cl_fix_round/cosim.py`……）。Python 的模块导入按「模块名」缓存，同名模块会互相覆盖。`cosim_runner` 用「临时改 `sys.path` + 全局线程锁」的方式，安全地按目录逐个加载这些同名脚本。

#### 4.3.2 核心流程

`cosim_runner` 有两个阶段：

```text
阶段 A — 构造（__init__，注册 TB 时发生一次）：
  1. 记录 enable / cosim_path / module_name(默认 "cosim")
  2. 创建一把「实例级」局部锁 self.lock（保证 run 最多执行一次）
  3. 拿「全局锁」COSIM_PATH_THREADLOCK，临时把 cosim_path 插到 sys.path[1]
  4. runpy.run_module("cosim") 执行脚本顶层代码，拿到 module_dict
       —— 注意：这会执行脚本里的 def/class/赋值，但不会调用 run() 函数
  5. 从 sys.path 移除 cosim_path（还原）
  → 此后 self.module_dict 里就持有了该脚本的 run() 和（可能的）COSIM_CONFIG

阶段 B — 运行（run，作为 pre_config 被调用，可能并发）：
  1. if not self.enable: 直接返回 True（快速路径，不抢锁）
  2. with self.lock:                          # 抢实例锁
       3. if self.enable:                      # 二次检查（等锁期间别人可能已关掉它）
            4. self.module_dict["run"]()        # 真正执行 cosim
            5. self.enable = False              # 自禁用
  6. return True                                # 告诉 VUnit「pre_config 成功」
```

这就是经典的 **double-checked locking**（双重检查锁定）模式：第一道 `if` 让「已禁用」的常见情况不必抢锁；第二道 `if` 防止「在排队等锁期间，别的线程已经把它关掉了」的竞态。

#### 4.3.3 源码精读

**全局线程锁**——保证「改 `sys.path` + 导入同名 `cosim.py`」这个临界区一次只进一个线程：

[sim/cosim_runner.py:25-27](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/cosim_runner.py#L25-L27) — 注释点明：这把锁让「把某 cosim 目录加到 path 并执行」成为独占操作，从而允许各运算的 `cosim.py` 同名而不冲突。

**构造函数：导入模块但不跑 run()**。关键在注释——`runpy.run_module` 执行的是脚本的**顶层代码**（定义函数、构造 `COSIM_CONFIG` 等），而真正的 cosim 入口 `run()` 函数此时只是被定义、并未被调用：

[sim/cosim_runner.py:31-48](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/cosim_runner.py#L31-L48) — 用 `sys.path.insert(1, ...)` 而非 `append`，是为了优先匹配本目录的 `cosim.py`；执行完立刻 `sys.path.remove` 还原，把对全局 path 的污染降到最小。

**get_config：暴露枚举信息（可选）**。如果某 cosim 脚本在顶层定义了 `COSIM_CONFIG` 字典，这里能把它取出来，供 `run.py` 据此动态生成 test 配置：

[sim/cosim_runner.py:50-57](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/cosim_runner.py#L50-L57) — 注意一个**本仓库的重要细节**：en_cl_fix 自己的 `sim/run.py` **并没有调用** `get_config()`，而是用显式的 `for ... get_tests(...)` 循环来枚举；真正用到 `COSIM_CONFIG` 的是它依赖的**基础库** `lib/en_tb`——那里的 `sim/run.py` 用 `get_config()["N_TESTS"]` 来决定 test 数量（见 `lib/en_tb/sim/run.py:74` 与 `lib/en_tb/bittrue/cosim/en_tb_fileio/cosim.py:54-55`）。所以 `get_config` 是为更通用的 en_tb 体系预留的接口，en_cl_fix 本身走的是更直接的循环写法。

**run：双重检查锁 + 自禁用**——本文件的核心：

[sim/cosim_runner.py:59-72](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/cosim_runner.py#L59-L72) — 三个要点：(1) 第一道 `if self.enable` 是「不抢锁」的快速路径；(2) `with self.lock` 内的第二道 `if` 处理「等锁期间被别人关掉」的竞态；(3) `self.enable = False` 实现「执行一次后永久自禁用」。最后**必须 `return True`**，否则 VUnit 会认为 pre_config 失败而中止该 test。

`self.enable` 的初值由 `disable` 参数取反得到（`run.py` 传入的是 `args.disable_cosim`）。所以命令行加 `--disable-cosim` 时，所有 `run()` 都走快速路径直接返回，**完全不生成黄金参考文件**——这在「只想验证 TB 能否编译/例化、不想等 cosim 跑完」时很有用。

#### 4.3.4 代码实践

**实践目标**：用一个最小化的「示例代码」复现 `cosim_runner` 的「只跑一次」保证，亲眼看到并发调用下函数体只执行一次。

**操作步骤**：

1. 把下面的「示例代码」存成 `cosim_lock_demo.py`（**这是教学示例，不是项目源码**）：

   ```python
   # 示例代码：演示 cosim_runner 的 double-checked locking 思想
   from threading import Lock, Thread

   class OnceRunner:
       def __init__(self):
           self.enable = True
           self.lock = Lock()
           self.call_count = 0          # 统计真正执行的次数

       def run(self):
           if self.enable:              # 第一道检查：快速路径
               with self.lock:          # 抢锁
                   if self.enable:      # 第二道检查：防等锁期竞态
                       self.call_count += 1   # 模拟“执行 cosim”
                       self.enable = False    # 自禁用
           return True

   runner = OnceRunner()
   threads = [Thread(target=runner.run) for _ in range(50)]
   for t in threads: t.start()
   for t in threads: t.join()
   print("实际执行次数 =", runner.call_count)   # 预期：1
   ```

2. 运行：`python cosim_lock_demo.py`。

**需要观察的现象**：50 个线程并发调用 `run()`，但 `call_count` 始终为 1。

**预期结果**：打印 `实际执行次数 = 1`。若去掉两道 `if self.enable` 与自禁用，`call_count` 会变成 50——这就直观说明了锁的必要性。

> 说明：本示例无需项目源码、无需仿真器，任何装有标准库的 Python 都能跑。

#### 4.3.5 小练习与答案

**练习 1**：`run()` 里为什么要有**两道** `if self.enable`，删掉第一道会怎样？
**答案**：第一道在锁外，让「已禁用」的常见情况不必抢锁、提升并发吞吐；删掉它，每次 `pre_config` 都要抢锁，但正确性不变（第二道仍在）。删掉第二道则**会出错**：等锁期间别的线程可能已执行并禁用，本线程拿到锁后会重复执行一次。

**练习 2**：所有运算的 cosim 脚本都叫 `cosim.py`，为什么不会互相覆盖？
**答案**：因为构造函数在**全局锁**保护下，只**临时**把当前目录插进 `sys.path`，`runpy.run_module` 后立刻移除（[cosim_runner.py:44-48](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/cosim_runner.py#L44-L48)），且只取 `module_dict` 而不把模块注册进 `sys.modules`，所以同名的下一个脚本能被干净地再次加载。

**练习 3**：`run()` 为什么必须 `return True`？
**答案**：VUnit 约定 `pre_config` 回调返回真值才表示「准备成功」；返回假值会让 VUnit 把该 test 标记为失败/跳过（见 [cosim_runner.py:71-72](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/cosim_runner.py#L71-L72) 的注释）。

---

## 5. 综合实践

**任务**：完整描述一次「`python sim/run.py --simulator=ghdl`」从启动到结束的时序，把本讲三个模块串起来。请在本地有 GHDL 时实际运行验证；没有 GHDL 时改为「精读脚本 + 时序画图」。

### 实际运行路径（有 GHDL 时）

1. **启动与搭台**：`python sim/run.py --simulator=ghdl --simulator-path <dir>`。
   - `import common` 触发 `common.py`：解析参数、校验、设 `VUNIT_SIMULATOR=ghdl`、定 `vhdl_standard_rtl=2008`、`vhdl_standard_tb=2008`。
2. **构建 test suite**：`run.py` 的 `create_test_suite` 注册 `en_tb`、`lib` 两库与全部 11 个 TB，并为每个 TB 构造专用 `cosim` 实例（继承自 `cosim_runner`）。**构造阶段**已用 `runpy.run_module` 把每个 `cosim.py` 的顶层代码执行了一遍（拿到 `run()` 引用，但未调用）。
3. **编译**：`vu.main()` 让 VUnit 按 GHDL 选项（`-frelaxed` 等）编译全部 VHDL。
4. **pre_config（仿真前，每个 test 配置触发一次）**：例如跑 `cl_fix_round_tb` 的 `MetaWidth=0` 配置时，VUnit 调用 `cl_fix_round_cosim.run()`：
   - 首次调用：抢锁 → 执行 `module_dict["run"]()` → 该 `run()`（见 u8-l1 的 `cl_fix_round/cosim.py`）穷举格式/舍入模式，用 Python 算黄金参考，写入 `bittrue/cosim/cl_fix_round/data/` 下的 `test{N}_output.txt`、`a_fmt.txt`、`r_fmt.txt`、`rnd.txt` → `self.enable=False`。
   - 同 TB 的 `MetaWidth=8` 配置随后触发同一 `run()`：因 `enable` 已为 `False`，直接返回 `True`，**不重复生成**。
5. **仿真（TB 对拍）**：`cl_fix_round_tb` 读取 `data/` 下刚写好的格式/输出文件，用同一计数规则重生成输入，调用 VHDL `cl_fix_round` 函数，逐条与黄金参考比对（详见 u8-l3）。
6. **post_run（全部 test 结束后一次）**：`common.post_run` 合并覆盖率（GHDL 无覆盖率支持，故此处基本是空操作；仅 modelsim/questa 真正合并 `.ucdb`）。

### 源码阅读路径（无 GHDL 时）

按以下顺序精读，画出时序图：

1. [sim/run.py:239-252](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L239-L252)（入口 → `vu.main`）。
2. [sim/run.py:56-74](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L56-L74)（cosim 工厂 + add TB 的 pre_config 绑定）。
3. [sim/cosim_runner.py:59-72](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/cosim_runner.py#L59-L72)（run 的双重检查锁）。
4. 对照 u8-l1 的 `bittrue/cosim/cl_fix_round/cosim.py` 的 `run()`，确认它在 pre_config 阶段被调用、写出的文件正是 TB 要读的。

**预期产出**：一张包含「import common → create_test_suite → 编译 → pre_config(cosim) → 仿真(TB 对拍) → post_run」五个阶段的时序图，并标注 cosim 的「最多一次」自禁用发生在哪两个配置之间。

> 待本地验证：上述时序中的并发行为（多个 TB 是否同时 pre_config）取决于 VUnit 的 `--parallel` 设置；本仓库默认串行，但 `cosim_runner` 的锁设计已为并行留好余地。

## 6. 本讲小结

- `sim/common.py` 是验证地基：import 即执行，负责解析命令行参数、选定仿真器、设置 `vhdl_standard_rtl/tb` 两个常量，并提供 `post_run` 覆盖率回调；VUnit 本身由 pip 的 `vunit-hdl` 提供。
- `sim/run.py` 是总调度：把 RTL（`hdl/`）、en_tb 扩展（`tb/util/`）、TB（`tb/`）按各自 VHDL 标准编入 `lib` 库，并为 11 个运算 TB 注册 test 配置；其中 round/saturate/resize 用「一个 test + 多 config」展开 `meta_width_g=0/8`。
- `pre_config` 钩子是把「Python 写黄金参考」和「VHDL 读文件对拍」缝合的关键：每个 TB 配置仿真前都会调用对应 cosim 的 `run`。
- `sim/cosim_runner.py` 用「双重检查锁 + 自禁用」保证每个 cosim 脚本在一整轮仿真里**最多执行一次**，即使 VUnit 并发跑多个 test 也不会重复生成或争写 `data/`。
- 它还用「全局锁 + 临时改 sys.path」安全加载多个同名的 `cosim.py`，并通过 `get_config()`（被基础库 en_tb 使用）暴露 `COSIM_CONFIG` 枚举信息。
- 命令行 `--disable-cosim` 可让所有 `run()` 走快速路径直接返回，跳过黄金参考生成，便于快速验证编译。

## 7. 下一步学习建议

- 下一讲 **u8-l3「VHDL testbench 模式与文件 I/O」**会从 VHDL 一侧补齐闭环：精读 `tb/cl_fix_*_tb.vhd` 如何读取本讲生成的 `data/` 文件、重生成输入、调用 VHDL 函数并逐位比对，以及 `tb/util/en_cl_fix_fileio_pkg.vhd` 如何包装 en_tb 的文件 I/O。建议先把本讲的时序图记牢，再到 u8-l3 里把「TB 读文件」这一格填上。
- 若想深入 VUnit 本身，可阅读其官方文档对 `pre_config`/`post_run`、`add_config`、`--parallel` 的说明，对照本讲理解 en_cl_fix 为何需要 `cosim_runner` 的并发保护。
- 进阶可对比 `lib/en_tb/sim/run.py`（基础库）与 `sim/run.py`（本工程）的异同——前者用 `get_config()["N_TESTS"]` 动态枚举，后者用显式循环，这是「通用库 vs 专用工程」两种取舍的好例子。
