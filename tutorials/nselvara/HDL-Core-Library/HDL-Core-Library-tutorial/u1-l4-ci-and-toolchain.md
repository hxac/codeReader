# 工具链配置与持续集成

## 1. 本讲目标

上一讲我们学会了「在本地用 `test_runner.py` 跑一次仿真」。本讲把视角从**单台开发机**抬升到**整个团队的工程化**：编辑器如何知道那些厂商库在哪里、代码每次 push 到 GitHub 后谁来负责验证、CI 用的仿真器和本地又有什么不同。

学完后你应该能够：

1. 看懂 `vhdl_ls.toml` 如何为 VHDL-LS / TerosHDL 声明 `vunit_lib` / `osvvm` / `UNISIM` / `xpm` / `altera_mf` 这些库以及它们对应的源码文件。
2. 读懂 `.github/workflows/vunit.yml` 这条 CI 流水线：它如何安装 Xilinx / Intel 仿真库、如何用 `nvc --install` 把厂商库装进仿真器、如何跑测试并产出报告。
3. 理解 NVC 这个开源仿真器在 CI 里扮演的角色，以及它相比商业 ModelSim/QuestaSim 的局限。
4. 区分本地用的 `test_runner.py` 与 CI 专用的 `test_runner_ci_cd.py`，尤其是 `use_intel_altera_libs` 与 `excluded_list` 这两处关键差异。

---

## 2. 前置知识

在进入源码前，先用几句话建立三个直觉。

**第一，什么叫「工具链」与「CI」。** 工具链（toolchain）指把源码变成可验证结果的一整套工具：编辑器、语言服务器（LSP）、仿真器、库文件。持续集成（Continuous Integration，CI）指每次代码 push 或 PR 时，由云端机器自动跑一遍工具链，把「在我电脑上能跑」变成「在团队的标准环境里能跑」。本项目的 CI 由 GitHub Actions 驱动，定义在 `.github/workflows/` 下的 YAML 文件里。

**第二，为什么 HDL 项目的工具链特别麻烦。** 普通 Python/JS 项目 `pip install` 一下就有库可用；但 VHDL 项目要同时面对「语言服务器要能找到库才能补全和查错」「仿真器要能绑定厂商原语（primitive）才能跑」两件事。本项目用到的厂商库有三家：Xilinx 的 `UNISIM` / `xpm`、Intel 的 `altera_mf`，加上验证框架 VUnit 的 `vunit_lib` 和 OSVVM 的 `osvvm`。这些库的源码文件散落在不同安装目录里，需要一份「地图」告诉工具去哪里找——这份地图就是 `vhdl_ls.toml`。

**第三，开源仿真器 NVC 与商业仿真器的差别。** 本地通常用商业的 ModelSim / QuestaSim；CI 上为了免费、可复现，改用开源的 [NVC](https://github.com/nickg/nvc) 仿真器。NVC 完整支持 VHDL-2008，但它有一个关键限制：**不能在 VHDL 代码里直接使用厂商用 Verilog 写的原语**（参见后文 `test_runner_ci_cd.py` 的注释）。这正是 CI 要排除 PLL 模块、要用行为级模型替代 Xilinx 原语的根本原因。

> 承接上一讲：上一讲我们建立了 `test_runner.py → run_all_testbenches_lib（子模块）→ VUnit → 仿真器` 的三层调用链。本讲会看到，CI 把这条链的「最上层包装器」从 `test_runner.py` 换成 `test_runner_ci_cd.py`，把「仿真器」从 ModelSim 换成 NVC，其余结构完全一致。

---

## 3. 本讲源码地图

本讲涉及的关键文件，按「配置 → 流水线 → 脚本」的层次排列：

| 文件 | 作用 | 给谁用 |
|------|------|--------|
| `vhdl_ls.toml` | 声明语言服务器要加载哪些库、它们的源码文件在哪 | VHDL-LS / TerosHDL（编辑器，本地） |
| `.github/workflows/vunit.yml` | 定义 CI 流水线：装环境、装库、跑测试、发报告 | GitHub Actions（云端） |
| `ip/test_runner_ci_cd.py` | CI 专用的 VUnit 包装器，排除 PLL、开启双厂商库、输出 xunit | CI 流水线调用 |
| `ip/test_runner.py` | 本地通用包装器（对比用，上一讲已讲） | 开发者本地 |

另外涉及两个被克隆的第三方仓库（CI 里临时拉取、用完即删，不在本仓库内）：`nselvara/gplgpu`（提供 Intel/Quartus 仿真库）、`nselvara/grlib`（提供 Xilinx UNISIM 的开源 VHDL 版本）。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**`vhdl_ls.toml` 库声明** → **NVC 仿真器** → **`vunit.yml` CI 流水线** → **`test_runner_ci_cd.py`**。顺序上先讲「库怎么声明」，再讲「仿真器怎么吃下这些库」，然后串成一条完整 CI 流水线，最后聚焦 CI 脚本与本地脚本的差异。

---

### 4.1 vhdl_ls.toml 库声明

#### 4.1.1 概念说明

[VHDL-LS](https://github.com/VHDL-LS/Rust_hdl) 是一个用 Rust 写的语言服务器（Language Server）。你在 VS Code 里装上 `VHDL-LS` 扩展或 `TerosHDL` 扩展后，它就在后台分析你的 `.vhd` 文件，提供跳转、补全、实时报错。但 VHDL-LS 默认不知道两件事：

1. **哪个文件属于哪个库（library）。** VHDL 里 `use work.xxx` 和 `use vunit_lib.xxx` 指向不同的逻辑库，必须告诉它「`vunit_lib` 这个名字对应磁盘上哪些文件」。
2. **第三方库的文件在磁盘哪里。** 比如 OSVVM 装在 Python venv 的 `site-packages` 里，Xilinx 库装在 Vivado 安装目录下。

`vhdl_ls.toml` 就是用 TOML 格式写的一份「库 → 文件」映射表。文件开头明确说明了它的用途与跨平台前提：

[vhdl_ls.toml:1-3](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L1-L3) —— 说明这份文件告诉 VHDL-LS 库和工作目录文件在哪里，并要求主目录下有一个名字含 `venv` 的虚拟环境。

#### 4.1.2 核心流程

整个文件由一个 `[libraries]` 表构成，每个库用一个「库名.字段」的键来声明，主要有三种字段：

- `库名.files = [ ... ]`：一个 glob 数组，列出该库包含的所有 `.vhd` 文件。
- `库名.exclude = [ ... ]`：从 `files` 里剔除的文件（通常用于排除老标准版本）。
- `库名.is_third_party = true`：标记为第三方库，VHDL-LS 对这类库**不做严格检查**，只提供符号用于跳转。

下面是它的逻辑结构（伪配置）：

```
[libraries]
vunit_lib.files   = [ venv 里的 VUnit 源码 ]   + 排除 93/2002/2019 老标准
vunit_lib.is_third_party = true

osvvm.files       = [ venv 里的 OSVVM 源码 ]   + 排除老标准 + Aldec 专用 + _c 文件
osvvm.is_third_party = true

defaultlib.files  = [ ./ip/**/*.vhd ]          # ← 本项目自己的全部 VHDL

UNISIM.files      = [ Xilinx unisims 目录 ]      # Windows + Linux 两套路径
UNISIM.is_third_party = true

xpm.files         = [ Xilinx xpm 目录 ]
xpm.is_third_party = true

altera_mf.files   = [ Intel altera_mf_components.vhd ]
altera_mf.is_third_party = true
```

注意一个关键点：**本项目自己的源码被放进 `defaultlib`，且没有 `is_third_party`**——也就是说 VHDL-LS 会对我们写的每一行 VHDL 做严格检查，而厂商库只是「只读参考」。

#### 4.1.3 源码精读

**验证框架库 vunit_lib。** 它的源码来自 Python venv 安装的 `vunit-hdl` 包，文件里有 Windows 风格（`Lib`）和 Linux 风格（`lib/python*`）两套 glob，并用 `exclude` 剔除三个老 VHDL 标准的实现：

[vhdl_ls.toml:7-16](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L7-L16) —— 声明 `vunit_lib`，把 venv 里 VUnit 的 VHDL 源码纳入，排除 `*2019*` / `*2002*` / `*93*` 三个老标准版本，并标记为第三方库。

**OSVVM 随机化库。** 与 vunit_lib 类似，但 exclude 规则更细：除了老标准，还要排除 `*Aldec*`（Aldec 仿真器专用文件）和 `*_c*`（C 接口相关文件）：

[vhdl_ls.toml:18-29](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L18-L29) —— 声明 `osvvm`，含 Windows/Linux 两套路径，排除老标准、Aldec 专用与 `_c` 文件。

**本项目自己的代码。** 注意它没有 `is_third_party`，所以会被严格 lint：

[vhdl_ls.toml:31-33](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L31-L33) —— `defaultlib` 用 `./ip/**/*.vhd` 收纳本项目全部设计源码与测试台。

**三家厂商库。** Xilinx 的 `UNISIM` 和 `xpm` 各给了 Windows（`C:/Xilinx/Vivado/...`）与 Linux（`/opt/...`）两套候选路径，Linux 路径旁还特意标了 `# NOTE: Set the correct path!`，提示读者这是占位符、必须改成自己机器的真实安装目录：

[vhdl_ls.toml:35-49](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L35-L49) —— 声明 `UNISIM`（unisims 目录）与 `xpm`（XPM IP 目录），均含 Windows/Linux 双路径与「请设置正确路径」提示。

Intel 的 `altera_mf` 则指向单个 `altera_mf_components.vhd` 文件，给的是 Quartus 安装目录下的仿真库路径：

[vhdl_ls.toml:51-57](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/vhdl_ls.toml#L51-L57) —— 声明 `altera_mf`，指向 Intel Quartus / Questa FSE 下的 `altera_mf_components.vhd`。

> 一个值得注意的不一致：README「Technology Support」一节里写的 Linux CI 路径是小写的 `/opt/xilinx/vivado/...`（见 README 原文），而真实 CI 与 `vhdl_ls.toml` 里用的是带版本号的 `/opt/xilinx/Vivado/2023.1/...`。**一律以真实文件为准**。

#### 4.1.4 代码实践

**实践目标**：让 VHDL-LS 在你自己的机器上正确识别 Xilinx UNISIM 库。

**操作步骤**：

1. 打开 `vhdl_ls.toml`，找到 `UNISIM.files` 那段（第 35–40 行）。
2. 如果你装了 Vivado（Windows），确认 `C:/Xilinx/Vivado/<你的版本>/data/vhdl/src/unisims/` 这个目录确实存在；把 glob 里的 `*` 与你的实际版本对应。
3. 如果你在 Linux 上、把 Vivado 装在 `/opt/Xilinx/Vivado/2023.1/`，把第 38–39 行的占位路径 `/opt/data/vhdl/src/unisims/` 改成 `/opt/Xilinx/Vivado/2023.1/data/vhdl/src/unisims/`（与 `xpm` 段第 46 行的风格保持一致）。
4. 保存后在 VS Code 里随便打开一个 `ip/memories/fifo/fifo_sync.vhd`，看 VHDL-LS 是否还对 `xpm` 相关符号报「unresolved」。

**需要观察的现象**：路径改对之前，编辑器对厂商原语（如 `xpm_fifo_sync`）报红色波浪线、提示找不到符号；改对之后红线消失，并能跳转到厂商源码。

**预期结果**：UNISIM / xpm 的未解析告警清零（前提是你确实安装了对应 Vivado 版本）。若你未安装 Vivado，可只保留行为级（`own_behavioural_*`）相关文件不报错即可。

**待本地验证**：具体路径取决于你本机 Vivado 安装版本，无法在此给出唯一答案。

#### 4.1.5 小练习与答案

**练习 1**：`vunit_lib` 为什么要 `exclude` 掉 `**/*93*.vhd`、`**/*2002*.vhd`、`**/*2019*.vhd`？

> **参考答案**：VUnit 会为多个 VHDL 标准各提供一份实现文件。本项目统一用 VHDL-2008，让语言服务器只加载 2008 版本可以避免「同一实体多份定义」的歧义，也加快分析速度。

**练习 2**：为什么 `defaultlib` 没有 `is_third_party = true`，而其他库都有？

> **参考答案**：`defaultlib` 装的是本项目自己写的、需要被严格检查的代码；第三方库（VUnit / OSVVM / 厂商库）只需作为「只读参考」供跳转，不需要 VHDL-LS 对它们做严格的 lint 与报错。

---

### 4.2 NVC 仿真器

#### 4.2.1 概念说明

[NVC](https://github.com/nickg/nvc) 是 Nick Gasson 开发的开源 VHDL 仿真器，目标是「快速、完整地支持 VHDL-2008」。它的地位是：**让一个没有任何商业 EDA license 的开源项目也能在 CI 上跑仿真**。本项目在本地可以选 ModelSim / QuestaSim（上一讲的 `VUNIT_MODELSIM_PATH` 就是给它们用的），但在 GitHub Actions 的免费 runner 上，唯一现实的选择就是 NVC。

NVC 的一个核心限制写在 CI 脚本注释里：

> "NVC cannot directly use Verilog primitives in VHDL code"（NVC 不能在 VHDL 代码里直接使用 Verilog 写的原语）

Xilinx 很多硬原语（如 `PLLE2_BASE`）在 Vivado 里是 Verilog 实现。NVC 是纯 VHDL 仿真器，没法绑定 Verilog 模块，所以 CI 必须绕开这些原语——要么用开源的 VHDL 行为级模型替代，要么干脆排除相关测试（见 4.4 节的 PLL 排除）。

#### 4.2.2 核心流程

NVC 在 CI 里的使用分三步：

1. **安装仿真器本体**：用社区 Action `nickg/setup-nvc@v1` 把 NVC 二进制装到 runner。
2. **把厂商库编译进 NVC 的全局缓存**：用 `nvc --install <库名>` 命令。NVC 会读取环境变量 `XILINX_VIVADO` / `QUARTUS_ROOTDIR` 指向的厂商安装目录，把里面的 VHDL 源码分析（analyze）后存入自己的库缓存，之后仿真时就能用 `-L vivado` 之类的参数引用。
3. **跑仿真**：由 VUnit 在后端调用 NVC（`vunit_hdl` 支持把 NVC 作为仿真器后端）。

关键命令是这三条（位置见 4.3.3）：

```
nvc --install xpm_vhdl    # 编译 Xilinx XPM 的 VHDL 版到 NVC
nvc --install quartus     # 编译 Intel/Quartus 仿真库到 NVC
nvc --install vivado      # 编译 Xilinx UNISIM 等到 NVC
```

每条命令背后，NVC 都需要厂商库的 VHDL 源码、以及一份 `vhdl_analyze_order` 文件（告诉它编译顺序）。CI 脚本里有大量 `touch .../vhdl_analyze_order` 就是为了满足这个要求。

#### 4.2.3 源码精读

NVC 本体的安装在流水线里只用一行 Action：

[.github/workflows/vunit.yml:31-33](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L31-L33) —— 用 `nickg/setup-nvc@v1` 安装最新版 NVC。

把厂商库装进 NVC 的三条命令位于那个大 `run` 块的末尾：

[.github/workflows/vunit.yml:76-79](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L76-L79) —— `nvc --version` 确认可执行，随后 `nvc --install xpm_vhdl`、`quartus`、`vivado` 把三家厂商库分别编译进 NVC 缓存。

而「NVC 不能用 Verilog 原语」这条限制，正写在被它调用的 CI 脚本里：

[ip/test_runner_ci_cd.py:36-38](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L36-L38) —— CI 脚本自己打印的策略说明：用 Xilinx 原语的行为级模型来仿真，因为 NVC 无法在 VHDL 里直接用 Verilog 原语。

#### 4.2.4 代码实践

**实践目标**：亲手把一个厂商库装进 NVC，体会 `--install` 做了什么。

**操作步骤**（源码阅读型实践，因需要 Linux + 厂商库源码）：

1. 阅读 `.github/workflows/vunit.yml` 第 54–74 行，理解 CI 是如何先 `git clone grlib`、把其中的 `unisim_VPKG.vhd` / `unisim_VCOMP.vhd` 复制到 `/opt/xilinx/Vivado/2023.1/data/vhdl/src/unisims/`，再 `touch` 一堆 `vhdl_analyze_order` 空文件的。
2. 思考：如果 `nvc --install vivado` 时某个目录缺少 `vhdl_analyze_order`，会发生什么？
3. （可选）在有 NVC 的 Linux 环境里执行 `nvc --install --help`，观察它对「安装路径」「分析顺序文件」的依赖说明。

**需要观察的现象**：`vhdl_analyze_order` 是 NVC 用来决定「先编译哪个文件」的清单；缺了它，`--install` 会因无法确定编译顺序而失败。

**预期结果**：能口头解释「为什么 CI 要 `touch` 五处 `vhdl_analyze_order`」——因为 GRLIB 只提供了部分 UNISIM 文件，NVC 的安装脚本仍会去那些子目录找顺序清单，空文件能骗过它的目录扫描，避免报错中断。

**待本地验证**：实际报错信息取决于 NVC 版本，需在本机/CI 日志中确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CI 选 NVC 而不是 ModelSim？

> **参考答案**：GitHub Actions 免费运行器没有商业 EDA license，无法运行 ModelSim/QuestaSim；NVC 是开源、免费、支持 VHDL-2008 的仿真器，适合 CI 这种需要可复现、零成本的环境。

**练习 2**：`nvc --install vivado` 和 `vhdl_ls.toml` 里声明 `UNISIM` 是同一件事吗？

> **参考答案**：不是。`nvc --install` 是**仿真时**把厂商库编译进 NVC 的库缓存，供仿真器绑定原语；`vhdl_ls.toml` 是**编辑时**告诉语言服务器去哪里找符号，供跳转和 lint。两者服务不同工具，但都依赖厂商库源码在磁盘上存在。

---

### 4.3 vunit.yml CI 流水线

#### 4.3.1 概念说明

[GitHub Actions](https://docs.github.com/en/actions) 用 YAML 文件描述流水线。`.github/workflows/vunit.yml` 就是本项目的 CI 定义。一条流水线（workflow）由若干 job 组成，每个 job 跑在一台虚拟机（runner）上，内部又分成若干 step（步骤）。本项目的流水线极简：只有一个 job `test`，跑在 `ubuntu-latest` 上。

理解这条流水线的关键，是把它读成一个**「从零搭出一个能仿真的 Linux 环境」的脚本**——因为 runner 每次都是干净的，Vivado、Quartus、NVC、VUnit 都得现装。

#### 4.3.2 核心流程

把 `vunit.yml` 的 `test` job 按步骤拆解，是一条清晰的流水：

```
触发：push 到 ip/** 或本 yml、以及所有 PR
  │
  ▼
1. checkout（含 git submodule: recursive）        ← 拉本仓库 + vhdl_utils 子模块
2. setup Python 3.9
3. setup-nvc（装 NVC 仿真器）
4. 同一大 run 块内：
     a. clone gplgpu → 拷 Intel/Quartus 仿真库到 /opt/intelFPGA/...
     b. touch 一堆空的 *_atoms/components.vhd（绕开 NVC 对缺失文件的报错）
     c. clone grlib → 拷 Xilinx UNISIM 的 VHDL 版到 /opt/xilinx/Vivado/2023.1/...
     d. touch 一堆空的 vhdl_analyze_order（满足 NVC --install）
     e. nvc --install xpm_vhdl / quartus / vivado
5. pip install vunit-hdl==5.0.0.dev6
6. 校验 nvc 与 vunit 版本
7. mkdir test-reports
8. 跑测试：python ./ip/test_runner_ci_cd.py --xunit-xml=test-reports/vunit_results.xml
     （环境变量 VUNIT_CI_MODE=true）
9. 生成 xunit-viewer 报告 / 发布测试结果 / 上传 artifact
```

注意两个环境变量的作用（第 81–82 行与 101–104 行）：`QUARTUS_ROOTDIR` 和 `XILINX_VIVADO` 既给 `nvc --install` 用（让它知道厂商库在哪），也给 VUnit/NVC 后端用。`VUNIT_CI_MODE=true` 则被 `test_runner_ci_cd.py` 读取，用来切换测试搜索路径。

#### 4.3.3 源码精读

**触发条件与权限。** 只在 `ip/**` 或本 yml 有改动时、以及任何 PR 上触发，避免无关改动浪费 CI 资源；权限里特别申请了 `checks: write` 和 `pull-requests: write`，是为了让后续「发布测试结果」步骤能在 PR 上挂评论：

[.github/workflows/vunit.yml:3-14](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L3-L14) —— 触发条件与 GitHub token 权限声明。

**Checkout 必须递归拉子模块。** 上一讲强调过：`vhdl_utils` 是 git 子模块，不拉下来 `test_runner` 就 import 失败。CI 在 checkout 时加了 `submodules: recursive`：

[.github/workflows/vunit.yml:21-24](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L21-L24) —— `actions/checkout@v4` 带 `submodules: recursive`，确保 `ip/vhdl_utils` 被拉取。

**最复杂的「装厂商库」步骤。** 这是整条流水线最长的一段。它先处理 Intel 侧（克隆 `gplgpu`、拷贝 `sim_lib`、`touch` 一批空的 atoms/components 文件），再处理 Xilinx 侧（克隆 `grlib`、建目录、拷 `unisim_VPKG.vhd` 与 `unisim_VCOMP.vhd`、建符号链接、`touch` 五个 `vhdl_analyze_order`），最后跑三条 `nvc --install`：

[.github/workflows/vunit.yml:34-82](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L34-L82) —— 在一个 `run` 块里同时配置 Intel 与 Xilinx 仿真库，并用 `env` 注入 `QUARTUS_ROOTDIR` / `XILINX_VIVADO` 两个路径环境变量。

其中，建立符号链接那行很关键——因为 NVC 的 vivado 安装脚本会去找一个叫 `unisim_retarget_VCOMP.vhd` 的文件，而 GRLIB 只提供了 `unisim_VCOMP.vhd`，于是用软链接「伪装」一份：

[.github/workflows/vunit.yml:66-67](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L66-L67) —— 把 `unisim_VCOMP.vhd` 软链接成 `unisim_retarget_VCOMP.vhd`，以兼容 NVC 安装脚本对文件名的固定预期。

**固定 VUnit 版本。** 注意 CI 把 VUnit 钉死在 `5.0.0.dev6`，而本地 `requirements.txt` 用的是 `>=5.0.0.dev5` 的范围。CI 钉版本是为了可复现，避免某天 VUnit 上游改了行为导致 CI 莫名失败：

[.github/workflows/vunit.yml:84-86](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L84-L86) —— `pip install vunit-hdl==5.0.0.dev6`，精确版本。

**跑测试与产出报告。** 先删掉临时克隆的 `gplgpu` / `grlib`（保持工作区干净），再用 `VUNIT_CI_MODE=true` 调用 CI 专用脚本，并通过 `--xunit-xml` 把结果写成 JUnit XML。后续三个步骤分别用这个 XML 生成可视化报告、把结果作为 check 发到 PR、上传为 artifact：

[.github/workflows/vunit.yml:96-105](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L96-L105) —— 实际跑测试的步骤，注入 `VUNIT_CI_MODE=true`，并设了 15 分钟执行超时。

[.github/workflows/vunit.yml:107-126](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.github/workflows/vunit.yml#L107-L126) —— 三个报告步骤：xunit-viewer 可视化、`publish-unit-test-result-action` 发 PR check、`upload-artifact` 存档。注意三者都带 `if: always()`，即使测试失败也会生成并发布报告。

#### 4.3.4 代码实践

**实践目标**：读懂一次真实 CI 运行的日志结构。

**操作步骤**（源码阅读 + 在线观察型实践）：

1. 打开 GitHub 仓库页面，点顶部 **Actions** 标签，进入 "VUnit Tests" workflow。
2. 任选一次最近成功的运行（绿勾），点进去看 `test` job 的日志。
3. 对照 `vunit.yml` 的步骤顺序，在日志里找到这四个关键输出：
   - `nvc --version`（确认 NVC 装好，约第 76 行对应处）。
   - 三条 `nvc --install ...` 的输出（看它编译了哪些文件、有无 warning）。
   - `VUnit version:`（第 91 行对应处）。
   - `=== CI/CD Test Results ===` 与 `HDL Tests: Passed`（来自 `test_runner_ci_cd.py`）。
4. 再点开一次**失败**的运行（红叉），观察报告步骤是否仍因 `if: always()` 而执行、PR 上是否挂了测试结果评论。

**需要观察的现象**：成功运行里，`nvc --install vivado` 会打印一串 `analysing ...` 的文件列表；失败运行里，测试报告和 artifact 依然被生成。

**预期结果**：能对照源码说出每段日志由 yml 的哪个 step 产生，并能解释 `if: always()` 的价值——失败也能看到报告，便于定位。

#### 4.3.5 小练习与答案

**练习 1**：为什么「跑测试」步骤单独设了 `timeout-minutes: 15`，而 job 整体设了 `timeout-minutes: 30`？

> **参考答案**：整体 30 分钟是兜底，防止任何步骤卡死；测试步骤单独给 15 分钟是为了更细粒度地控制——如果仿真本身卡住（比如某测试台挂死），能更快触发超时并报错，而不会把 30 分钟全耗在「装环境」之外的事情上。

**练习 2**：如果某次 push 只改了 `README.md`，CI 会跑吗？

> **参考答案**：不会。`on.push.paths` 只列了 `ip/**` 和 `.github/workflows/vunit.yml`，纯文档改动不会触发这条流水线，节省资源。

---

### 4.4 test_runner_ci_cd.py

#### 4.4.1 概念说明

这是 CI 专用的 VUnit 包装器，和本地用的 `test_runner.py` 是「双胞胎」——两者都调用同一个子模块函数 `run_all_testbenches_lib`，区别只在于传给它的参数。之所以要分两个脚本，是因为 CI 环境与本地环境有几个本质不同：

1. **CI 同时装了 Xilinx 和 Intel 两家库**（本地通常只有一家），所以要开 `use_intel_altera_libs=True`。
2. **CI 用 NVC，跑不了 PLL**（NVC 无法绑定 `PLLE2_BASE` 这个 Verilog 原语），所以要把 PLL 相关文件塞进 `excluded_list`。
3. **CI 需要机器可读的测试报告**，所以要从命令行解析 `--xunit-xml` 参数并传下去。
4. **CI 的测试路径基准不同**，靠 `VUNIT_CI_MODE` 环境变量切换。

#### 4.4.2 核心流程

脚本的执行流程：

```
入口 run_all_testbenches()
  │
  ├─ 读环境变量 VUNIT_CI_MODE → 决定 test_path（CI 用 "./"，本地用 "./ip/"）
  ├─ 从命令行解析 --xunit-xml 参数
  ├─ 构造 excluded_list = ["tb_pll.vhd", "pll.vhd"]
  │       └─ 原因：NVC 缺 PLLE2_BASE 的 VHDL 绑定
  ├─ 调用 run_all_testbenches_lib(...)，传入全部参数
  └─ 打印 === CI/CD Test Results === 与 Passed/Failed
```

`run_all_testbenches_lib` 来自子模块 `vhdl_utils`（上一讲已讲），它内部才会真正创建 VUnit 实例、发现并运行测试。本脚本只负责「替 CI 把参数配好」。

#### 4.4.3 源码精读

**CI 模式与测试路径切换。** 通过环境变量 `VUNIT_CI_MODE` 判断，CI 模式下用当前目录 `"./"` 作为搜索根（因为 CI 在仓库根目录执行，且前面 `rm -rf ./gplgpu ./grlib` 后工作区相对干净），本地则用 `"./ip/"`：

[ip/test_runner_ci_cd.py:22-26](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L22-L26) —— 读 `VUNIT_CI_MODE` 决定 `test_path`，CI 用 `"./"`，否则 `"./ip/"`。

**解析 xunit-xml 命令行参数。** 这段手工解析 `sys.argv` 的代码，是为了把 yml 里 `--xunit-xml=test-reports/vunit_results.xml` 的路径取出来再透传给底层：

[ip/test_runner_ci_cd.py:28-33](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L28-L33) —— 在 `sys.argv` 里找 `--xunit-xml` 并取其后一个参数作为输出路径。

**PLL 排除清单。** 这是本模块最关键的一处。`tb_pll.vhd` 与 `pll.vhd` 被排除，原因直接写在注释里——`PLLE2_BASE` 缺少 VHDL 绑定：

[ip/test_runner_ci_cd.py:45-48](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L45-L48) —— `excluded_list` 排除 PLL 的设计与测试台，注释说明是因 `PLLE2_BASE` 缺 VHDL 绑定。

**调用底层库时与本地脚本的差异。** 把 CI 脚本的调用（下表左）与本地 `test_runner.py`（下表右，参见 [ip/test_runner.py:20-32](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L20-L32)）逐参数对比：

| 参数 | CI（`test_runner_ci_cd.py`） | 本地（`test_runner.py`） | 差异原因 |
|------|------------------------------|--------------------------|----------|
| `path` | `"./"`（CI 模式） | `"./ip/"` | CI 在仓库根执行 |
| `use_xilinx_libs` | `True` | `True` | 都需要 glbl（见上一讲） |
| `use_intel_altera_libs` | **`True`** | **`False`** | CI 同时装了 Intel 库 |
| `excluded_list` | **`["tb_pll.vhd","pll.vhd"]`** | **`[]`** | CI 用 NVC，跑不了 PLL |
| `xunit_xml` | 命令行解析的路径 | `None` | CI 要产报告 |

CI 版的完整调用代码：

[ip/test_runner_ci_cd.py:50-62](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner_ci_cd.py#L50-L62) —— 把上述参数交给 `run_all_testbenches_lib`，其中 `use_intel_altera_libs=True` 与非空 `excluded_list` 是与本地脚本最显著的两处不同。

#### 4.4.4 代码实践

**实践目标**：解释清楚「为什么 CI 必须排除 PLL」，并理解 `excluded_list` 的作用。

**操作步骤**：

1. 打开 `ip/pll/pll.vhd`，找到 Xilinx 架构里例化 `PLLE2_BASE` 的那一行（这是 Xilinx 时钟硬原语）。
2. 回忆 4.2 节：NVC 是纯 VHDL 仿真器，无法绑定 Verilog 实现的 `PLLE2_BASE`。
3. 推理：如果 CI 不排除 `tb_pll.vhd`，`tb_pll` 例化 `pll`、`pll` 例化 `PLLE2_BASE` → NVC 找不到该原语的 VHDL 绑定 → 编译/绑定阶段报错 → 整条 CI 失败。
4. 写下一句话回答：「为什么 `excluded_list` 里同时有 `tb_pll.vhd` 和 `pll.vhd`？」

**需要观察的现象 / 预期结果**：你能用因果链讲清「NVC 的限制 → 必须排除 PLL → 否则 CI 红」。参考答案：即使只排除测试台 `tb_pll.vhd`，VUnit 在编译阶段仍会尝试分析所有被发现的源码；若 `pll.vhd` 也被纳入编译，`PLLE2_BASE` 仍会触发未绑定错误，所以两个文件都要排除，确保 PLL 相关代码完全不参与 CI 编译。

> 进一步思考：这与上一讲提到的「PLL 是全库唯一无自研行为级实现的 IP」是一体两面——正因为没有纯 VHDL 的行为级 PLL 可用，CI 才只能整体排除它，而不是像别的 IP 那样切到 `own_behavioural_*` 架构绕开厂商原语。

#### 4.4.5 小练习与答案

**练习 1**：如果你新写了一个依赖 Xilinx `BUFGCE` 原语的模块，CI 能跑通吗？要不要加进 `excluded_list`？

> **参考答案**：通常能跑通。`BUFGCE` 属于 UNISIM，CI 已通过 `grlib` 装了 UNISIM 的开源 VHDL 版（见 4.3 节），NVC 能绑定它。只有像 `PLLE2_BASE` 这种 GRLIB 没提供 VHDL 模型的硬原语才需要排除。所以一般不用加 `excluded_list`，但应在 CI 上实际验证。

**练习 2**：为什么 CI 脚本要自己解析 `--xunit-xml`，而不是让 VUnit 直接读 `sys.argv`？

> **参考答案**：因为本脚本在 `run_all_testbenches_lib`（子模块）之上又包了一层，VUnit 并不直接面对命令行。脚本需要先把 `--xunit-xml` 从 `sys.argv` 里取出来，再作为 `xunit_xml=...` 关键字参数传给底层，底层再交给 VUnit。这样 yml 里 `python test_runner_ci_cd.py --xunit-xml=...` 的写法才能生效。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「**读懂一次 CI 故障**」的小任务。

**场景**：假设你提了一个 PR，给 `ip/memories/fifo/fifo_sync.vhd` 加了新功能，CI 的 "VUnit Tests" check 变红了。

**任务步骤**：

1. **定位报告**。在 PR 的 check 里点 "VUnit Test Results"（由 `publish-unit-test-result-action` 发布，对应 yml 第 114–119 行），找到是哪个 `run("...")` 用例失败。
2. **下载 artifact**。点 Actions → 对应运行 → 下拉 `test-results` artifact（对应 yml 第 121–126 行），解压看 `vunit_results.xml` 的详细失败栈。
3. **区分失败类型**。判断它是：
   - **真功能 bug**（断言失败）→ 去读对应测试台和设计源码。
   - **编译/绑定错误**（比如报 `PLLE2_BASE` 或某原语未绑定）→ 检查是否误用了一个 CI 装不了的厂商原语，考虑是否要加进 `excluded_list`，或为该 IP 补一个 `own_behavioural_*` 架构。
4. **用 vhdl_ls.toml 辅助**。在本地编辑器里打开相关文件，看 VHDL-LS 是否也报了相同符号的「unresolved」，借此确认是库声明问题还是代码问题。
5. **写一段复盘**：用一句话说明这次失败发生在「编辑器 lint / 编译绑定 / 仿真断言」的哪一层，以及 `vhdl_ls.toml`、`nvc --install`、`excluded_list` 这三者中哪一个与本故障相关。

**预期结果**：你能把一次 CI 失败，准确归因到工具链的某一环，并说出修复方向——这正是本讲想建立的全局判断力。

---

## 6. 本讲小结

- **`vhdl_ls.toml` 是编辑器侧的库地图**：用 `[libraries]` 把 `vunit_lib` / `ossvm` / `UNISIM` / `xpm` / `altera_mf` 的磁盘文件告诉 VHDL-LS，第三方库标 `is_third_party = true` 只供跳转，本项目代码放 `defaultlib` 接受严格检查。
- **NVC 是 CI 的开源仿真器**：完整支持 VHDL-2008，但无法绑定 Verilog 原语；CI 用 `nvc --install xpm_vhdl / quartus / vivado` 把厂商库编译进缓存。
- **`vunit.yml` 是一条「从零搭仿真环境」的流水线**：克隆 `gplgpu` 与 `grlib` 提供厂商库 VHDL 源码、`touch` 空文件绕开 NVC 缺失文件报错、固定 `vunit-hdl==5.0.0.dev6`、跑测后用 `if: always()` 保证报告必出。
- **`test_runner_ci_cd.py` 是 CI 专用包装器**：靠 `VUNIT_CI_MODE` 切路径、解析 `--xunit-xml`、开启 `use_intel_altera_libs=True`、并用 `excluded_list` 排除 NVC 跑不了的 PLL。
- **本地与 CI 的根本差异**：仿真器（ModelSim ↔ NVC）、厂商库（单家 ↔ 双家）、是否排除 PLL（不排除 ↔ 排除）、是否产报告（无 ↔ xunit XML）。
- **PLL 被排除的根因**：`PLLE2_BASE` 是 Xilinx Verilog 硬原语，GRLIB 没提供 VHDL 行为级模型，NVC 无法绑定，且 PLL 是全库唯一无 `own_behavioural_*` 实现的 IP。

---

## 7. 下一步学习建议

本讲完成了「项目概览」单元（u1）的最后一讲，你已经掌握了从「读」到「本地跑」再到「CI 自动跑」的完整工程化视角。接下来建议：

- **进入第 2 单元（u2）「核心设计模式：同一实体多厂商架构」**：本讲反复出现的 `UNISIM` / `xpm` / `altera_mf` 与 `own_behavioural_*` 三套实现，将在 u2-l1 里被正式讲解为「同一 entity 多 architecture」模式——这是理解后续所有 IP 的钥匙。
- **在阅读 u2 前**，可以先随手打开 `ip/memories/fifo/fifo_sync.vhd`，数一数它有几个 `architecture`，各自 `use` 了哪家厂商库，作为 u2 的热身。
- **想深入验证方法学**的读者，可跳到第 11 单元（u11）「验证方法学」，那里会详细拆解 VUnit 测试台结构、OSVVM 随机化与 `.do` 波形脚本，与本讲的 CI 闭环形成首尾呼应。
