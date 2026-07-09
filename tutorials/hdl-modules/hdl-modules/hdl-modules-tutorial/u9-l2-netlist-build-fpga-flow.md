# Netlist 综合与 FPGA 构建流程

## 1. 本讲目标

本讲解决一个问题：**hdl-modules 用哪些脚本把 VHDL 变成可量化的资源数字？两个脚本各管什么？**

学完后你应该能够：

- 区分 `tools/synthesize.py`（任意实体的快速 netlist 综合）与 `tools/build_fpga.py`（统一的回归/完整构建入口）的职责。
- 看懂 `--generic name=v1,v2,...` 是如何用笛卡尔积展开成多组构建的。
- 解释为什么「快速反馈」脚本不带资源检查器，而「回归」脚本必须把每个工程的资源数精确断言。
- 说出 `tools_env.py` 定义的几条路径（`REPO_ROOT` / `HDL_MODULES_DIRECTORY` / `HDL_MODULES_GENERATED`）在两个脚本里各自扮演的角色。

本讲承接 [u1-l3 工具链与依赖](u1-l3-toolchain-and-deps.md)（工具链与 PYTHONPATH 引导套路），并与 [u8-l3 资源占用回归](u8-l3-resource-utilization-regression.md)（`get_build_projects` + `build_result_checkers`）正反相承：u8-l3 讲「每个模块怎么声明回归工程」，本讲讲「这些工程由谁收集、谁来跑、以及一个不写回归、只为临时看资源数字的旁路工具」。

## 2. 前置知识

本讲需要你已建立下列认知（来自前置讲义，这里只做最简回顾，不重复展开）：

- **netlist（网表）/ 综合与实现的区别**：综合（synthesis）把 VHDL 翻译成门级/原语级网表；实现（implementation）再继续做布局布线。本讲两个脚本都可以「只综合、不实现」，用最短时间拿到资源占用与逻辑级数反馈。详见 u8-l3 的 netlist 构建概念。
- **generic（类属参数）**：VHDL 编译期开关。综合时 generic 为假的功能对应的 `generate` 块会被删除，未启用特性零资源占用（见 [u1-l1](u1-l1-project-overview.md)）。
- **`module_*.py` 的两个钩子**：`setup_vunit` 登记仿真配置（见 [u8-l2](u8-l2-vunit-testbench-patterns.md)），`get_build_projects` 返回一组 netlist 构建工程（见 [u8-l3](u8-l3-resource-utilization-regression.md)）。本讲的 `build_fpga.py` 正是触发 `get_build_projects` 的入口。
- **资源回归**：对一组 generic 组合综合出 netlist，用 `build_result_checkers`（如 `TotalLuts(EqualTo(14))`）断言资源数精确等于已知正确值，偏离即构建失败（见 u8-l3）。
- **PYTHONPATH 引导套路**：每个入口脚本开头先 `sys.path.insert(0, REPO_ROOT)`，再 `import tools.tools_pythonpath` 把本地兄弟仓库检出优先于 pip 安装（见 u1-l3）。
- **tsfpga**：hdl-modules 的构建/仿真骨架依赖。本讲的两个脚本本身只写「工程收集」逻辑，真正驱动 Vivado 综合/实现的是 tsfpga 提供的 `setup_and_run()` 与 `arguments()`。

一个贯穿本讲的关键区分：

| 脚本 | 用途 | 工程来源 | 资源检查器 | 典型时机 |
| --- | --- | --- | --- | --- |
| `tools/synthesize.py` | 临时看某个实体综合后多大、多快 | 命令行 `--generic` 即时构造 | **无** | 你改完代码想立刻要反馈 |
| `tools/build_fpga.py` | 回归 / 完整构建 | 各模块 `get_build_projects()` 钩子 | **有**（netlist 构建） | CI、发布前 |

记住这张表，本讲其余部分都是在解释它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tools/synthesize.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py) | 把**任意**实体（命令行给出顶层名）按 `--generic` 组合快速综合为 netlist，做设计反馈；不挂资源检查器。 |
| [tools/build_fpga.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_fpga.py) | 统一构建入口：从所有模块的 `get_build_projects()` 钩子收集工程，可跑 netlist 回归，也可跑完整实现构建。 |
| [tools/tools_env.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tools_env.py) | 路径的单一信息源：`REPO_ROOT`、`HDL_MODULES_DIRECTORY`、`HDL_MODULES_GENERATED`、`HDL_MODULES_DOC`。两个脚本都 `from tools import tools_env`。 |
| [modules/fifo/module_fifo.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py) | `get_build_projects()` 的真实样例，`build_fpga.py` 会回调它。本讲用它佐证「工程来自模块钩子」。 |

## 4. 核心概念与源码讲解

### 4.1 tools_env.py：路径的单一信息源

#### 4.1.1 概念说明

无论是把 VHDL 综合成网表，还是跑仿真，脚本都需要知道三件事：仓库根在哪、模块源码在哪、生成的产物（综合工程、IP 缓存、文档）该往哪写。`tools_env.py` 把这三件事固化成几个常量，让 `synthesize.py`、`build_fpga.py`、`build_docs.py`、`simulate.py` 共享同一套路径定义——这样改一处即全局生效，避免每个脚本各自拼接路径而出现不一致。

它还有一个工程价值：**零外部依赖**。这个文件只 `from pathlib import Path`，不导入 tsfpga/VUnit，所以即便用户没装任何 FPGA 工具链，也能 `import tools_env` 拿到正确路径（这与 [u1-l3](u1-l3-toolchain-and-deps.md) 讲的「只用 src/ 可综合源码则零依赖」一脉相承）。

#### 4.1.2 核心流程

路径推导用「相对脚本自身位置」的方式，不依赖当前工作目录：

```text
tools_env.py 位于 <repo>/tools/tools_env.py
        │  Path(__file__)            # 当前文件路径
        │  .parent                   # tools/
        │  .parent                   # <repo>/
        ▼
REPO_ROOT  = <repo>/
HDL_MODULES_DIRECTORY = <repo>/modules/   # VHDL 模块源码主体
HDL_MODULES_GENERATED = <repo>/generated/ # 综合工程、IP 缓存、生成文档落点
HDL_MODULES_DOC       = <repo>/doc/       # 手写文档源
```

因为用 `resolve()` 取绝对路径，所以「你在哪个目录敲命令」不影响路径正确性。

#### 4.1.3 源码精读

整个文件只有四行实质定义：

[tools/tools_env.py:12-16](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tools_env.py#L12-L16) —— 用 `Path(__file__).parent.parent.resolve()` 锁定仓库根，再派生出模块目录、文档目录、生成目录。注意 `generated/` 不进 git（是构建产物），但它正是 `synthesize.py` 默认放综合工程的地方。

本讲最常用到的是 `HDL_MODULES_GENERATED`：两个脚本都把它作为 `default_temp_dir` 传给 tsfpga 的 `arguments()`，于是综合工程、Vivado IP 缓存等默认都落在 `<repo>/generated/` 下。

#### 4.1.4 代码实践

1. **目标**：确认四条路径在本机上指向真实存在的目录。
2. **步骤**：在仓库根目录启动 Python（无需安装 tsfpga）：

   ```bash
   python3 -c "from tools import tools_env as e; print(e.REPO_ROOT); print(e.HDL_MODULES_DIRECTORY.exists()); print(e.HDL_MODULES_GENERATED)"
   ```

3. **观察**：`REPO_ROOT` 是仓库绝对路径；`HDL_MODULES_DIRECTORY.exists()` 应为 `True`（因为 `modules/` 进了 git）；`HDL_MODULES_GENERATED` 可能还不存在（直到你第一次跑综合才会被创建）。
4. **预期**：输出三行，第二行为 `True`。
5. 若报 `ModuleNotFoundError: tools`，说明当前目录不是仓库根或没把仓库根加进 `PYTHONPATH`——这正是入口脚本开头那段 `sys.path.insert(0, str(REPO_ROOT))` 要解决的问题。

#### 4.1.5 小练习与答案

- **练习**：为什么 `tools_env.py` 用 `Path(__file__).parent.parent` 而不是 `os.getcwd()`？
  **答案**：`__file__` 锚定脚本自身位置，推导出的根与「你在哪个目录执行」无关；`getcwd()` 依赖调用者的当前目录，换个工作目录就失效，不可靠。
- **练习**：`HDL_MODULES_GENERATED` 指向的 `generated/` 目录为什么不应该提交进 git？
  **答案**：它是构建产物（综合工程、IP 缓存、生成的 HTML/C++/VHDL），可由源码重新生成；提交它会造成无意义的 diff 和仓库膨胀。

---

### 4.2 tools/synthesize.py：任意实体的快速 netlist 综合

#### 4.2.1 概念说明

`synthesize.py` 是「**临时反馈**」工具：你在命令行指定任意一个实体名（`top_level` 位置参数）和若干 `--generic` 取值，它就把这些组合分别综合成 netlist，让你立刻看到资源占用与（可选的）时序。它和回归流程（4.3 节）的关键差别有三：

1. **工程是当场构造的，不是模块声明的**——它不调用任何 `get_build_projects()` 钩子，而是用命令行的 `--generic` 直接在脚本里 `new` 出 `TsfpgaExampleVivadoNetlistProject`。
2. **不挂资源检查器**——它没有 `build_result_checkers`，综合完只产生报告，不判通过/失败。这很合理：临时探索时你还不知道「正确」的资源数应该是多少。
3. **固定器件、只综合**——器件写死为 Kintex UltraScale+ 的 `xcku5p-sfvb784-3-e`，且强制 `synth_only=True`（不跑实现），用最短路径拿到反馈。

适合的场景：改完一段代码，想立刻知道「开了某个 generic 会多耗多少 LUT」「换个位宽逻辑级数会不会变深」。这正是 u8-l3 里反复用 `tools/synthesize.py` 对照资源数字的依据。

#### 4.2.2 核心流程

```text
命令行:  python tools/synthesize.py fifo --generic width=8,32 --generic depth=1024
                  │                │             │
                  ▼                ▼             ▼
        位置参数 top_level   action="append"   也可重复出现
        = "fifo"            收集成 list
                                     │
                                     ▼
                    parse_generics(): 把 "--generic name=v1,v2" 解析成
                    [{name: v1}, {name: v2}]，多个 generic 取笛卡尔积
                                     │
                                     ▼
                    get_modules(): 扫描 modules/ 得到模块集合（决定源码与库）
                                     │
                                     ▼
                    对每组 generics 构造一个 TsfpgaExampleVivadoNetlistProject
                    （name = test_case_name(top, generics)，唯一）
                                     │
                                     ▼
                    BuildProjectList 包装 → setup_and_run(synth_only=True)
                                     │
                                     ▼
                    tsfpga 驱动 Vivado 只做综合，写出网表与利用率报告
```

`--generic` 的多组合展开是本脚本最值得精读的机制，单独在 4.2.3 拆开讲。

#### 4.2.3 源码精读

**入口 `main()`：解析 → 收集模块 → 构造工程 → 交给 tsfpga 跑。**

[tools/synthesize.py:32-48](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py#L32-L48) —— `main()` 的主体。注意第 36 行 `get_modules(modules_folder=tools_env.HDL_MODULES_DIRECTORY)` 用了 4.1 节的路径常量去扫描模块；第 38-48 行用一个**列表推导**对「每一组 generic」造一个工程，工程名由 `BaseModule.test_case_name(name=args.top_level, generics=generics)` 生成（把实体名和 generic 取值序列化成唯一名，避免不同组合覆盖彼此的输出目录）。器件写死在第 42 行 `part="xcku5p-sfvb784-3-e"`。

**为什么写死器件？** 资源数（LUT/FF/RAM/逻辑级数）随器件族而变。快速反馈工具选一个代表性器件即可；而回归（4.3 节）则必须把器件钉死到某个具体型号，否则「断言资源数等于 14」毫无意义（fifo 回归用的就是另一个器件 `xc7z020clg400-1`）。

**强制只综合：** [tools/synthesize.py:51-63](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py#L51-L63) —— 给通用函数 `setup_and_run` 填入本流程的固定参数：`synth_only=True`（不跑实现）、`num_threads_per_build=2`、`collect_artifacts_function=None`、`from_impl=False` 等。这样 hdl-modules 不必自己写 Vivado 调用逻辑，复用 tsfpga 的通用构建骨架即可。

**命令行参数：** [tools/synthesize.py:66-121](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py#L66-L121) —— `arguments()`。三个互斥的「只做一半」开关值得记住：

[tools/synthesize.py:72-84](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py#L72-L84) —— `--list-only`（只列出将构建的工程，不跑）、`--create-only`（只建工程不开综合）、`--open`（在 GUI 里打开已有工程）。它们构成互斥组，方便先看清「会展开成几个构建」再决定是否真跑。

[tools/synthesize.py:110-119](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py#L110-L119) —— 本讲的主角参数：`--generic`（别名 `--parameter`）用 `action="append"`，可重复出现；`top_level` 是位置参数。注意 `--generic` 的取值格式是 `name=value1,value2,...`，单条参数就能携带多个取值。

**`--generic` 的笛卡尔积展开：** [tools/synthesize.py:124-167](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py#L124-L167) —— `parse_generics()` 是脚本里信息密度最高的一段。它分两步：

1. 解析每条参数：[tools/synthesize.py:136-160](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py#L136-L160) —— 校验恰好一个 `=`、name/value 非空，然后把逗号分隔的每个 value 转型：`true/false` → 布尔、纯数字 → 整数，其余报错。结果攒成 `{"a": [True, False], "b": [16, 32]}` 这样的「每个 generic 一个取值列表」。
2. 求笛卡尔积：[tools/synthesize.py:162-167](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py#L162-L167) —— 用 `itertools.product(*result_raw.values())` 对所有 generic 的取值列表做笛卡尔积，再把每个组合 `zip` 回 generic 名，得到一组 dict：

\[
\{\,a\in\{T,F\}\,\}\times\{\,b\in\{16,32\}\,\}
\;=\;
\bigl\{(a{=}T,b{=}16),\,(a{=}T,b{=}32),\,(a{=}F,b{=}16),\,(a{=}F,b{=}32)\bigr\}
\]

所以 `--generic a=true,false --generic b=16,32` 一共会展开成 **2 × 2 = 4 组**构建，每组对应列表推导里的一个工程。这就是「`--generic name=v1,v2` 如何展开为多组构建」的全部答案：**逗号分隔给一个 generic 多个取值，多个 generic 之间取笛卡尔积**。

#### 4.2.4 代码实践

1. **目标**：用 `synthesize.py` 对 `fifo` 做两组 generic 组合的综合，对比资源报告；并解释 `--generic name=v1,v2` 的展开方式。
2. **步骤**（需本地装好 Vivado 与 tsfpga；未装则用 `--list-only` 验证展开逻辑即可）：

   先用 `--list-only` 看「会展开成几个工程」，不实际综合：

   ```bash
   python tools/synthesize.py fifo --list-only \
     --generic enable_last=true,false \
     --generic width=32 \
     --generic depth=1024
   ```

   预期列出 2 个工程（`enable_last` 取 true/false 各一个，`width` 与 `depth` 各只一个取值，故 2 × 1 × 1 = 2）。

   再实际综合两组并比较资源（去掉 `--list-only`）：

   ```bash
   python tools/synthesize.py fifo \
     --generic enable_last=true,false \
     --generic width=32 \
     --generic depth=1024
   ```

3. **观察**：综合产物默认落在 `<repo>/generated/projects/<工程名>/` 下；打开其中的 Vivado 利用率报告，对比两组的 LUT/FF/RAM 数。
4. **预期**：`enable_last=true` 与 `enable_last=false` 的资源数应**几乎相同**——这与 u4-l1 / u8-l3 的结论一致：`enable_last` 只把 RAM 字宽加 1 位，近乎免费。
5. **待本地验证**：本环境未必装有 Vivado，具体资源数字与报告文件名以本地综合输出为准；若只关注「展开逻辑」，`--list-only` 已足够且无需 Vivado。

#### 4.2.5 小练习与答案

- **练习**：命令 `python tools/synthesize.py fifo --generic width=8,32 --generic enable_packet_mode=true,false --generic depth=1024` 会展开成几组构建？
  **答案**：3 个 generic 各有 2、2、1 个取值，笛卡尔积为 2 × 2 × 1 = **4 组**。
- **练习**：`--generic foo=bar` 会发生什么？为什么？
  **答案**：报错 `Cannot parse "foo" generic value: "bar"`。因为 [parse_generics](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py#L150-L160) 只认 `true/false`（布尔）和纯数字（整数），`bar` 二者皆非。
- **练习**：为什么 `synthesize.py` 不给工程挂 `build_result_checkers`？
  **答案**：它是探索/反馈工具，综合时往往还不知道「正确」的资源数；检查器是回归流程（`build_fpga.py`）的职责，那里每个数字都已固化为期望值。

---

### 4.3 tools/build_fpga.py：统一构建入口与 get_build_projects 收集

#### 4.3.1 概念说明

如果说 `synthesize.py` 是「随手量一量」的尺子，`build_fpga.py` 就是「**正式度量与构建**」的流水线入口。它的工程不是当场构造的，而是向**每个模块**索取：调用 tsfpga 的 `get_build_projects(modules=...)`，后者会遍历所有模块、回调各自的 `get_build_projects()` 钩子，把声明好的 netlist 工程（带 `build_result_checkers`）汇总成一个列表。这正是 u8-l3 讲过的「资源回归工程从哪来」的答案——**从模块钩子来，由 `build_fpga.py` 统一收集与执行**。

它还支持「完整构建」模式（不止 netlist，还跑实现、生成 bitstream），由 tsfpga 的 `arguments()` 提供的开关控制；netlist 构建只是它能力的一个子集，专门服务于资源/时序回归。

#### 4.3.2 核心流程

```text
命令行:  python tools/build_fpga.py [--netlist-builds] [--project-filters fifo]
                  │
                  ▼
        get_modules(modules_folder=HDL_MODULES_DIRECTORY)  # 扫描模块
                  │
                  ▼
        get_build_projects(                              # tsfpga 汇总
            modules=modules,
            project_filters=args.project_filters,        # 可按模块名过滤
            include_netlist_not_full_builds=args.netlist_builds)  # 是否含 netlist 构建
                  │
                  ▼   回调每个模块的 get_build_projects()，例如 fifo 模块返回
                  │   一组 TsfpgaExampleVivadoNetlistProject，每个带 build_result_checkers
                  ▼
        BuildProjectList(projects=...) → setup_and_run(...)
                  │
                  ▼
        tsfpga 逐个工程：综合 → (按需) 实现；结束后比对 build_result_checkers，
        资源数偏离期望 → 构建失败（回归红线）
```

与 4.2 的对照要点：`build_fpga.py` 自身**不解析 generic、不写器件、不构造工程**——这些都已在每个模块的 `get_build_projects()` 里声明好了（见 u8-l3）。它只负责「收集 + 执行 + 检查」。

#### 4.3.3 源码精读

`build_fpga.py` 出奇地短，因为所有重活都委托给 tsfpga 了：

[tools/build_fpga.py:27-43](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_fpga.py#L27-L43) —— 整个 `main()`。三步走：第 28 行用 tsfpga 的 `arguments()` 解析命令行（注意它和 `synthesize.py` 用的 `arguments` 不同——这里直接 `from tsfpga.examples.build_fpga_utils import arguments`，带来 `--netlist-builds`、`--project-filters`、`--num-parallel-builds`、`--output-path` 等通用开关）；第 30 行扫描模块；第 31-37 行用 `get_build_projects(...)` 汇总工程列表；第 39-43 行交给同一个 `setup_and_run` 执行。

第 35 行的 `include_netlist_not_full_builds=args.netlist_builds` 是关键开关：netlist 构建工程（带资源检查器的那些）默认**不**包含在「完整构建」里，只有显式加 `--netlist-builds` 时才纳入——这把「快速资源回归」与「完整 bitstream 构建」拆成两条互不干扰的子流程，却共用同一个入口脚本。

**工程到底长什么样？** 看一个真实模块钩子即可。fifo 的 `get_build_projects()` 返回的第一组「最小 FIFO」工程：

[modules/fifo/module_fifo.py:116-130](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L116-L130) —— 方法签名与开头。注意第 125 行把器件钉死为 `xc7z020clg400-1`（Zynq-7000），与 `synthesize.py` 的 `xcku5p-...` 不同——回归必须固定器件，资源断言才有意义。

[modules/fifo/module_fifo.py:143-158](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L143-L158) —— 一个典型的 netlist 工程声明：顶层是 `fifo_netlist_build_wrapper`（只引裸端口的最小夹具，见 u8-l3），挂了五个 `build_result_checkers`：`TotalLuts(EqualTo(14))`、`Ffs(EqualTo(24))`、`Ramb36(EqualTo(1))`、`Ramb18(EqualTo(0))`、`MaximumLogicLevel(EqualTo(6))`。`build_fpga.py` 跑完综合后，会拿 Vivado 的实际利用率逐条比对这些期望值，任何一条不等就让该工程失败。这就是 u8-l3 所说「把面积/时序退化当 bug 防」的落地机制。

**为什么 `get_build_projects` 里要本地 import？** [modules/fifo/module_fifo.py:117-121](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L117-L121) 的注释解释了：`hdl_modules` 这个 Python 包在大多数使用场景下不在 PYTHONPATH 上（别人把 fifo 当源码用时不会装它），所以 `from hdl_modules import get_hdl_modules` 必须延迟到方法体内；而这个方法**只在** `build_fpga.py` 跑仓库内 netlist 构建时才会被调用，那时 PYTHONPATH 已经被入口脚本引导好了（见 u1-l3）。这是「钩子延迟导入」的典型模式。

#### 4.3.4 代码实践

1. **目标**：用 `build_fpga.py --list-only`（或 `--netlist-builds --list-only`）看清 fifo 模块声明了哪些回归工程，并对照检查器理解每个数字对应启用了哪些 generic。
2. **步骤**（`--list-only` 不实际综合，无需完整跑 Vivado，但需装 tsfpga）：

   ```bash
   # 只看 fifo 模块的 netlist 回归工程清单
   python tools/build_fpga.py --netlist-builds --project-filters fifo --list-only
   ```

   （`--list-only` 是否由 tsfpga 的 `arguments()` 提供，**待本地确认**；若该版本没有此开关，改用 `--create-only` 仅建工程不综合，同样能看到工程清单。）

3. **观察**：列出形如 `fifo.minimal`、`fifo.with_last`、`fifo.with_packet_mode`、`fifo.with_drop_packet`、`asynchronous_fifo.minimal` 等多个工程名。
4. **预期**：对照 [module_fifo.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py)，`fifo.minimal`（14 LUT / 24 FF）是基线；`fifo.with_last` 与基线资源**相同**（`enable_last` 免费，对应 [module_fifo.py:200-217](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L200-L217)）；而 `fifo.with_packet_mode`（40 LUT / 47 FF）因多一个包尾计数器而**上升**（对应 [module_fifo.py:219-237](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L219-L237)）。这复现了 u4-l1 / u8-l3 的资源结论。
5. **待本地验证**：具体工程名与开关名以本地 tsfpga 版本为准。

#### 4.3.5 小练习与答案

- **练习**：`build_fpga.py` 与 `synthesize.py` 的工程来源有何本质区别？
  **答案**：`synthesize.py` 用命令行 `--generic` **当场**构造工程（无检查器）；`build_fpga.py` 调用 `get_build_projects(...)` 从各模块的 `get_build_projects()` 钩子**收集**工程（带检查器）。
- **练习**：为什么 fifo 的 netlist 工程用 `xc7z020clg400-1`，而 `synthesize.py` 用 `xcku5p-sfvb784-3-e`？
  **答案**：回归工程必须固定器件族，资源断言（如 `TotalLuts(EqualTo(14))`）才有可重复的意义；fifo 选用 Zynq-7000 这个常见小器件。`synthesize.py` 只是快速反馈，选一个代表性器件即可，器件不同会导致资源数不同，故二者不能混用数字。
- **练习**：`--netlist-builds` 这个开关为什么是「可选包含」而非默认开启？
  **答案**：完整构建（生成 bitstream）和 netlist 回归是两种用途：前者面向「能不能上板」，后者面向「资源/时序有没有退化」。默认只跑用户明确要求的那一类，避免无谓地综合大量 netlist 或反过来误跑漫长的实现流程。

---

### 4.4 两条流程的分工与协作：共享 setup_and_run 的骨架

#### 4.4.1 概念说明

读完 4.2、4.3 你会发现：两个脚本的开头引导套路一样（`REPO_ROOT` → `insert` → `import tools_pythonpath`），结尾也一样（都把工程列表交给 tsfpga 的 `setup_and_run(...)`）。它们的差别全在「**中间那步工程从哪来**」：

- `synthesize.py`：工程 = 命令行 `--generic` 笛卡尔积 → 当场构造，无检查器，固定器件，强制 `synth_only`。
- `build_fpga.py`：工程 = 各模块 `get_build_projects()` 钩子汇总 → 带检查器，每工程自带器件，可 netlist 也可完整构建。

这种「自定义收集逻辑 + 共享执行骨架」的分工，正是 tsfpga 作为依赖的价值：hdl-modules 不必自己写 Vivado 工程创建、并行构建、报告解析，只写「我要综合哪些东西」。

#### 4.4.2 核心流程

两者的统一形状：

```text
[入口脚本]
   1. sys.path.insert(0, REPO_ROOT)            # 共享引导
   2. import tools.tools_pythonpath             # 优先本地兄弟仓库
   3. from tsfpga... import BuildProjectList, setup_and_run
   4. modules = get_modules(HDL_MODULES_DIRECTORY)   # 共享：扫描模块
   5. projects = <各自的方式收集工程列表>        # ← 唯一分叉点
   6. setup_and_run(modules, BuildProjectList(projects), args, ...)  # 共享执行
```

第 5 步是分叉：`synthesize.py` 用列表推导 + `parse_generics`；`build_fpga.py` 用 `get_build_projects(...)`。

#### 4.4.3 源码精读

**共享的引导头（两个文件几乎逐字相同）：**

[tools/synthesize.py:17-29](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py#L17-L29) 与 [tools/build_fpga.py:13-24](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_fpga.py#L13-L24) —— 都是「算 REPO_ROOT → insert → import tools_pythonpath → 再 import tsfpga 符号」。注释里那句 *Do PYTHONPATH insert() instead of append() to prefer any local repo checkout over any pip install*（见 [tools/synthesize.py:17](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py#L17)）是整个项目处理依赖的核心取向（详见 u1-l3 的 `tools_pythonpath`）。

**共享的执行尾巴：** 两个脚本都调用同一个 `setup_and_run(modules=..., project_list=..., args=..., collect_artifacts_function=None)`（[synthesize.py:59-63](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/synthesize.py#L59-L63) 与 [build_fpga.py:39-43](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_fpga.py#L39-L43)）。`synthesize.py` 在调用前额外塞了一组固定参数（`synth_only=True` 等，见 [4.2.3](#423-源码精读)），把通用函数「特化」成纯综合流程；`build_fpga.py` 则把是否 `synth_only` 的决定权留给 tsfpga `arguments()` 解析出来的命令行开关。这就是「同一骨架，两种用法」。

**生成的代码制品（寄存器）也在这一链路里被处理：** 注意 `synthesize.py` 第 53 行 `args.generate_registers_only = False`——这个参数名暗示 tsfpga 在创建工程前会先用 hdl-registers 从 toml 生成寄存器 VHDL（见 [u7-l3](u7-l3-dma-registers-and-cpp-driver.md) / [u9-l1](u9-l1-documentation-generation.md)）。两个构建脚本都自动获得「寄存器代码先于综合生成」的能力，无需用户手工跑 generator——这与 u1-l4 所说「寄存器生成在仿真/构建流程由 tsfpga 自动完成」一致。

#### 4.4.4 代码实践

1. **目标**：用 diff 视角对比两个脚本，亲手确认「引导头与执行尾相同，只有工程收集那段不同」。
2. **步骤**：

   ```bash
   diff -u tools/synthesize.py tools/build_fpga.py
   ```

3. **观察**：差异集中在 `main()` 的中段（一个 `parse_generics` + 列表推导，一个 `get_build_projects(...)`），以及 `synthesize.py` 多出来的 `arguments()`/`parse_generics()` 两个函数定义。
4. **预期**：文件头部的 PYTHONPATH 引导块、底部的 `setup_and_run(...)` 调用结构高度相似甚至逐字相同；这正说明二者共享 tsfpga 骨架。
5. 这是一个纯源码阅读型实践，不需要 Vivado。

#### 4.4.5 小练习与答案

- **练习**：如果想让 `synthesize.py` 也带上资源检查器（综合后断言 LUT 数），技术上缺什么？
  **答案**：缺「已知正确的期望值」。`synthesize.py` 面向探索，generic 组合由命令行临时给出，事先没有固化的期望资源数；而 `build_fpga.py` 的检查器是把「已经验证过一次的正确数字」写死在模块钩子里。要加检查器，得先跑一次拿到数字，再像 [module_fifo.py:150-156](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L150-L156) 那样写进 `build_result_checkers`。
- **练习**：两个脚本都先 `get_modules(...)` 再构建，为什么 `synthesize.py` 也需要模块集合（毕竟它只综合一个实体）？
  **答案**：一个实体通常 `use` 了别的库（如 fifo `use` common/math/resync），综合需要把所有相关源码与库都加入工程；`get_modules()` 扫描出的模块集合正是 tsfpga 用来决定「加哪些源文件、建哪些库、挂哪些约束、生成哪些寄存器」的依据。

## 5. 综合实践

把本讲三块知识串起来：用 **`synthesize.py`（4.2）做探索、用 `tools_env`（4.1）解释落点、对照 `build_fpga.py`（4.3）的回归**。

任务：评估「给同步 FIFO 开 `enable_packet_mode` 的资源代价」。

1. **用 `synthesize.py` 临时量**（需 Vivado；无则跳到第 3 步做源码阅读）：

   ```bash
   # 先确认会展开成 2 组（enable_packet_mode 取 true/false）
   python tools/synthesize.py fifo --list-only \
     --generic enable_packet_mode=true,false \
     --generic enable_last=true \
     --generic width=32 --generic depth=1024

   # 实际综合（强制 enable_last=true，因为 packet_mode 依赖它）
   python tools/synthesize.py fifo \
     --generic enable_packet_mode=true,false \
     --generic enable_last=true \
     --generic width=32 --generic depth=1024
   ```

   注意这里故意加 `enable_last=true`——因为 [fifo 的断言](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/src/fifo.vhd) 要求开 packet_mode 前必须先开 last（见 u4-l1）。若漏掉，VHDL 侧 `assert severity failure` 会让综合失败——这也是「generic 之间有依赖」的活教材。

2. **读资源**：综合产物在 `<repo>/generated/projects/`（由 `tools_env.HDL_MODULES_GENERATED` 决定，见 4.1.3）下的两个工程目录里，对比 `enable_packet_mode=true` 与 `false` 的 LUT/FF 差值。
3. **对照回归**：打开 [module_fifo.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py)，找到 `fifo.with_packet_mode`（[第 219-237 行](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L219-L237)，断言 40 LUT / 47 FF）与其上一档 `fifo.with_last`（[第 200-217 行](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L200-L217)，27 LUT / 35 FF）。回归固化值为：开 packet_mode 比 only-last 多约 13 LUT / 12 FF（一个包尾计数器的代价）。
4. **结论**：把你 `synthesize.py` 量出的差值与回归固化的差值对账，二者应在同一量级（注意器件不同：探索用 `xcku5p`、回归用 `xc7z020`，绝对数会不同，但「增量」可比较）。这一步把「探索工具」与「回归工具」的协作关系落到实处。
5. **待本地验证**：第 2 步的具体资源数字与报告文件名以本地 Vivado 输出为准。

## 6. 本讲小结

- `tools_env.py` 是路径单一信息源：`REPO_ROOT` / `HDL_MODULES_DIRECTORY` / `HDL_MODULES_GENERATED` / `HDL_MODULES_DOC`，零外部依赖，两个构建脚本都 `from tools import tools_env`。
- `tools/synthesize.py` 是**临时反馈**工具：用位置参数 `top_level` 指定任意实体，`--generic name=v1,v2,...`（可重复）的取值经 `itertools.product` 取笛卡尔积展开成多组工程，当场构造、不挂检查器、固定 Kintex 器件、强制 `synth_only=True`。
- `tools/build_fpga.py` 是**统一构建入口**：用 tsfpga 的 `get_build_projects(...)` 从各模块 `get_build_projects()` 钩子收集工程（带 `build_result_checkers`），`--netlist-builds` 控制是否纳入资源回归工程，支持 netlist 回归与完整实现构建两条子流程。
- 两个脚本共享同一段 PYTHONPATH 引导头与同一个 `setup_and_run(...)` 执行骨架，差别只在「工程列表从哪来」；寄存器 VHDL 也会在此链路自动生成（`generate_registers_only` 参数）。
- 「探索无检查器、回归必带检查器」不是疏漏而是分工：探索时尚无「正确数字」，回归则把已验证数字固化成红线，把面积/时序退化当 bug 防。
- 器件必须固定才有可比性：`synthesize.py` 用 `xcku5p-sfvb784-3-e`，fifo 回归用 `xc7z020clg400-1`，二者绝对资源数不可混用。

## 7. 下一步学习建议

- **回看 u8-l3**：现在你已经知道 `get_build_projects()` 的产物由 `build_fpga.py` 收集执行，可以重读 [u8-l3 资源占用回归](u8-l3-resource-utilization-regression.md)，把「模块声明工程 → 入口收集 → tsfpga 执行 → 检查器比对」整条链路在脑中走通。
- **阅读 [tools/simulate.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/simulate.py)**：它是仿真侧的对应入口，同样共享 PYTHONPATH 引导套路，触发的是 `setup_vunit` 钩子（见 [u8-l2](u8-l2-vunit-testbench-patterns.md)）。对比它和 `build_fpga.py`，能加深对「钩子 + 入口」对称结构的理解。
- **结合 u9-l1 与 u9-l3**：[u9-l1 文档生成](u9-l1-documentation-generation.md) 讲 `build_docs.py` 如何调用 hdl-registers generator；本讲提到的「构建时自动生成寄存器 VHDL」正是同一套 generator 在构建链路里的隐式触发。[u9-l3 发布与 Lint](u9-l3-release-lint-contributing.md) 则讲这些构建如何被纳入 CI。
- **进阶动手**：仿照 [module_fifo.py 的 `get_build_projects`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L116-L130)，给一个你熟悉的实体新增一个 netlist 回归工程，先用 `synthesize.py` 量出资源数，再把它固化成 `build_result_checkers`，最后用 `build_fpga.py --netlist-builds` 跑通——这会闭环本讲的全部概念。
