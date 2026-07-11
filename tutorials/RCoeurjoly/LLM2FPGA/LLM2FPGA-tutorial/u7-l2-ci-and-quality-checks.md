# CI 与代码质量门禁

## 1. 本讲目标

前几讲我们一直在关心「降级链能不能把模型正确降到底」。本讲换一个视角，回答一个工程化问题：

> 「这个仓库有 Nix 脚本、Python adapter、SystemVerilog RTL、Bash 降级脚本四种语言交织。我怎么保证每次提交（尤其是别人提交）都不会把其中某种语言写坏？又怎么保证那些『由生成器产生、不在 git 里』的文档不会和权威源脱节？」

LLM2FPGA 的答案分两层：一层是 **CI 里的 `nix flake check`**，它把四类静态检查汇成一条命令；另一层是 **`docs-md` 应用**，它把 Org 权威源转成 Markdown 派生物。两者都挂在 `flake.nix` 的 `checks` / `apps` 输出上，由 GitHub Actions 在每次 push/PR 时统一触发。

学完后你应当能做到：

- 说出 `.github/workflows/ci.yml` 的三个步骤，并解释它为什么**不调用任何单独的 lint 命令、只调用一条 `nix flake check`**。
- 列出 `flake.nix` 的 `checks` 输出里四个检查的名字（`nix` / `python` / `systemverilog` / `shell`）、各自用什么工具、检查什么性质的问题。
- 解释 `runCommand` 这个 Nix 惯用法是如何把「一个 shell 命令」变成「一个 CI 门禁」的——命令失败即派生失败，派生失败即 `nix flake check` 失败。
- 说清 `docs-md` 为什么只处理 `README.org` + `deliverables/*.org` + `docs/*.org` 这三类、而不递归扫描全仓库的 `.org`，以及「`.org` 为权威源、`.md` 为派生物」这条约定在脚本里是如何落实的。

本讲承接 [u1-l3](u1-l3-nix-reproducible-toolchain.md)（flake 的 inputs/devShell/outputs 三类产物）。u1-l3 讲了「flake 的 `outputs` 字段长什么样」，本讲深入其中两个此前一笔带过的字段：`checks` 和 `apps`。

## 2. 前置知识

阅读本讲前，你需要先建立以下几个概念（均来自前置讲义，这里只做一句话回顾）：

- **flake 的 `outputs`**：一个 flake 把所有对外暴露的东西都放在 `outputs` 里，包括 `packages`（构建产物）、`devShells`（开发环境）、`apps`（可执行命令）、`checks`（测试/门禁）、`formatter`（格式化器）等（见 u1-l3）。
- **`runCommand`**：Nix 的一个函数，把一段 shell 命令包成一个**派生（derivation）**。命令成功（exit 0）才产出一个 store 路径 `$out`；命令失败则整个派生失败。本讲里它被复用为「把任意 lint 命令变成一个 CI 门禁」的统一模具（见 u3-l5 讲过的 `runCommand` 包装）。
- **`writeShellApplication`**：Nix 的另一个函数，把一段 bash 脚本包成一个**带固定 `runtimeInputs` 的可执行程序**，工具依赖被钉进派生、不污染调用方环境。`docs-md` 就是这么造出来的。
- **`.org` 权威源**：仓库里 `.org` 文件是文档的「唯一真相源」，`.md` 是给不用 Org mode 的读者自动生成的派生物，**永不手改 `.md`**（见 u1-l2）。
- **降级链产出物**：`matmulSv`（matmul 降级到 SystemVerilog 后的派生，内含 `sources.f`）、`tbDataSv`（PyTorch 黄金参考生成的 `tb_data.sv`）——本讲的 SystemVerilog lint 检查会复用这两个产物作为输入（见 u4-l1、u4-l2）。

如果以上某条你还很陌生，建议先回到对应讲义。本讲不再重复其细节，只承接其结论。

## 3. 本讲源码地图

本讲涉及的关键文件，按「从触发到执行」排列：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `.github/workflows/ci.yml` | **CI 触发器**，GitHub Actions 入口 | 三个 step：checkout → install nix → `nix flake check`，为何如此之短 |
| `flake.nix` 的 `checks`（L807–L862） | **四个质量门禁的定义** | 每个检查的名字、`nativeBuildInputs`、执行的命令、失败语义 |
| `flake.nix` 的 `docsMdApp`（L608–L627）与 `apps.docs-md`（L800–L805） | **文档生成器** | `writeShellApplication` 如何钉住 pandoc、只处理三类 `.org`、产物落到原目录 |
| `README.md`（L111–L118） | **文档格式约定的口头说明** | 「`.org` 为权威源、`.md` 为派生物」这条约定的人读版本 |

一句话定位：**`ci.yml` 是扳机，`checks` 是弹匣里的四发子弹，`docs-md` 是另一个独立的工具应用**。本讲的篇幅主要花在「拆弹匣」上——看清四发子弹各自打什么。

## 4. 核心概念与源码讲解

### 4.1 nix flake check：把所有质量门禁汇成一条命令

#### 4.1.1 概念说明

很多项目的 CI 长这样：在 workflow 里手写一长串 `run:` 块，`make lint`、`ruff check`、`verilator --lint`、`shellcheck`……每加一个检查就改一次 workflow 文件。这种写法的问题是：**CI 与本地能跑的命令不一致**——开发者本地很难一次复现 CI 的全部检查。

Nix flake 提供了一个更好的机制：**`checks` 输出**。flake 的 `outputs.checks` 是一个 attrset，每个属性就是一个「派生」。`nix flake check` 这一条命令会：

1. 枚举 `outputs.checks` 里的**每一个**派生；
2. 逐个构建它们（在当前系统上）；
3. 任意一个构建失败，`nix flake check` 就以非零退出码结束。

也就是说，**「质量门禁」在 Nix 的世界观里就是「一个普通的派生」**——不需要专门的 lint 框架，凡是能在 Nix 里被构建的东西，都能被 `nix flake check` 汇总。CI 端因此可以退化成一句话。

这个设计的直接收益有三：

- **本地与 CI 同源**：开发者本地跑 `nix flake check` 和 CI 跑的完全一样，不存在「本地过、CI 挂」的环境差异。
- **工具版本钉死**：每个 check 是个派生，它的 `nativeBuildInputs`（如 `statix`、`verilator` 的具体版本）被 flake 钉死，不会因 CI runner 升级而突然行为变化。
- **新增检查零成本**：在 `checks` 里加一条属性，CI 自动覆盖，不用动 `ci.yml`。

#### 4.1.2 核心流程

```
GitHub: push 或 pull_request
   │
   ▼
.github/workflows/ci.yml 的 checks job
   ├─ step 1: actions/checkout@v4        （拉代码）
   ├─ step 2: cachix/install-nix-action@v30  （装 Nix，开 flakes）
   └─ step 3: LC_ALL=C nix flake check     （唯一的质量命令）
                    │
                    ▼
        枚举 flake.nix 的 outputs.checks.<system>:
            ├─ nix           （deadnix + statix + nixfmt）
            ├─ python        （py_compile 全仓 .py）
            ├─ systemverilog （verilator --lint-only）
            └─ shell         （shellcheck 全仓 .sh）
                    │
        任一派生构建失败 ──▶ nix flake check 退出码非零 ──▶ CI 红
        全部成功 ──▶ CI 绿
```

关键点：CI 文件里**没有**任何一条形如 `deadnix .`、`verilator --lint` 的具体命令——这些细节全部封在 `checks` 派生内部，`ci.yml` 只负责「装好 Nix、调一条 `nix flake check`」。这是一种「**薄 CI、厚 flake**」的分工。

#### 4.1.3 源码精读

**先看 CI 入口**——它短得几乎不需要解释：

[.github/workflows/ci.yml:1-6](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/.github/workflows/ci.yml#L1-L6) —— workflow 名为 `CI`，触发条件是任意 `push` 或 `pull_request`。注意 `on:` 下没有任何分支过滤，意味着**任何分支的 push、任何 PR 都会触发**——这是项目「所有改动都要过质量门」的态度。

[.github/workflows/ci.yml:8-9](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/.github/workflows/ci.yml#L8-L9) —— 只有一个 job `checks`，跑在 `ubuntu-latest` 上。整个仓库的 CI 就这一个 job。

[.github/workflows/ci.yml:11-17](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/.github/workflows/ci.yml#L11-L17) —— 前两个 step 是标配：`actions/checkout@v4` 拉代码；`cachix/install-nix-action@v30` 装 Nix，并用 `extra_nix_config` 开启 `experimental-features = nix-command flakes`（ flakes 仍是实验特性，必须显式打开，这是 u1-l3 提过的细节）。

[.github/workflows/ci.yml:18-20](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/.github/workflows/ci.yml#L18-L20) —— **整个 CI 的核心就这一行**：`LC_ALL=C nix flake check`。`LC_ALL=C` 强制用 POSIX locale，避免某些 Nix 工具在非英文 locale 下输出/解析异常（例如 `statix`、`nixfmt` 的错误信息排序）。这一行不带任何 flake URI，默认检查当前目录的 `flake.nix`。

**再看 `checks` 在 flake 里的总骨架**：

[flake.nix:807-862](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L807-L862) —— `checks = { ... }` 这个 attrset 就是 `nix flake check` 枚举的对象。它有且仅有四个属性：`nix`、`python`、`systemverilog`、`shell`。每一个都是一个 `pkgs.runCommand "llm2fpga-<lang>" { ... } '' ... ''` 派生——即「跑一段 shell，成功才产出 `$out`」。本节先看清这个总骨架，四个检查的内部细节留到 4.2 逐个拆。

注意每个 check 的产物目录名都是 `llm2fpga-<lang>`（如 `llm2fpga-nix`、`llm2fpga-python`），且每个命令末尾都有 `mkdir -p "$out"`——这是**让 `runCommand` 成功产出路径的惯例**。`runCommand` 要求命令最终在 `$out` 处留下点什么（文件或空目录都行），否则即使命令 exit 0，Nix 也会因为「`$out` 不存在」而判失败。所以这行 `mkdir -p "$out"` 不是装饰，而是「检查通过」的物理凭证。

#### 4.1.4 代码实践

**实践目标**：在本地复现 CI 的全部行为，验证「薄 CI、厚 flake」——你能在本地用同一条命令得到和 CI 完全一致的结果。

**操作步骤**：

1. 在仓库根目录执行（需先 `nix develop` 或系统已装 Nix）：
   ```bash
   LC_ALL=C nix flake check
   ```
2. 观察输出：Nix 会逐个构建 `checks` 里的四个派生，每个会打印一行 `nix-shell:> building '<...>'` 之类的进度。
3. 若想只跑某一个 check 而不跑全部（调试时常用），用：
   ```bash
   nix build .#checks.x86_64-linux.nix --no-link
   ```
   （把 `nix` 换成 `python` / `systemverilog` / `shell` 即可单跑。`x86_64-linux` 是你的系统三元组，macOS 上是 `aarch64-darwin` 等。）

**需要观察的现象 / 预期结果**：

- 全部通过时，`nix flake check` 末尾无报错、退出码 0。
- 单跑某一项时，若该 check 失败，`nix build` 会打印失败派生的完整日志（包括 lint 工具的具体报错行号），比 CI 网页上更易读。
- 如果你故意在某 `.sh` 文件里写一个明显错误（如未引用的变量），单跑 `.#checks.x86_64-linux.shell` 应当让 shellcheck 报出对应位置——这验证了「单跑 = 全量检查的一部分」。

> 注：本实践需本地有 Nix。若环境无 Nix，可只读 [flake.nix:807-862](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L807-L862) 并口述「这四个属性会被 `nix flake check` 逐个构建」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ci.yml` 里没有 `run: deadnix .`、`run: shellcheck` 之类的具体 lint 命令？

**答案**：因为这些命令被封装进 `checks` 派生里了。`nix flake check` 会枚举 `outputs.checks` 的所有属性并逐个构建，每个属性内部跑什么命令由 `flake.nix` 决定，与 `ci.yml` 无关。CI 只负责「装 Nix + 调一条 `nix flake check`」，具体检查清单完全由 flake 管理。这样设计的好处是：CI 配置极薄、不易腐化；新增/移除检查只改 `flake.nix` 不动 CI；本地与 CI 同源（见 4.1.1）。

**练习 2**：`ci.yml` 第 20 行的 `LC_ALL=C` 去掉会怎样？

**答案**：大多数情况下不会出错，但存在风险——某些 lint 工具（如 `statix`、`nixfmt`）在非 POSIX locale 下的报错信息排序、字符处理可能不一致，导致 CI 在不同 runner locale 配置下行为微妙不同。`LC_ALL=C` 把 locale 钉死成最朴素的 POSIX locale，消除这一不确定性。这是「把环境不确定性钉死」的工程习惯，与项目用 Nix 钉死工具版本是同一思路。

**练习 3**：如果我想新增一个检查（比如用 `ruff` 检查 Python 风格），要改哪些文件？

**答案**：**只改 `flake.nix`**，不用动 `ci.yml`。在 `checks` attrset 里加一条属性，例如 `python-style = pkgs.runCommand "llm2fpga-python-style" { nativeBuildInputs = [ pkgs.ruff ]; } '' cd ${./.} && ruff check . && mkdir -p "$out" ''`。下次 `nix flake check` 自动会跑它，CI 也自动覆盖。这正是「薄 CI、厚 flake」的红利。

---

### 4.2 四语言静态 lint：Nix / Python / SystemVerilog / Shell

#### 4.2.1 概念说明

`checks` 里的四个检查对应仓库里的四种语言。它们有一个共同结构——都是「`runCommand` + 某个 lint 工具 + `mkdir -p $out`」，但每个工具检查的**性质**不同，值得分别理解：

| 检查名 | 语言 | 工具 | 检查性质 | 失败意味着 |
| --- | --- | --- | --- | --- |
| `nix` | Nix | `deadnix` + `statix` + `nixfmt --check` | 死代码 + 反模式 + 格式 | Nix 代码有未使用绑定、有坏味道、或未按 `nixfmt` 格式化 |
| `python` | Python | `python311` 内置 `compile()` | 仅语法（byte-compile） | 某 `.py` 文件有语法错误（不检查类型、不运行） |
| `systemverilog` | SystemVerilog | `verilator --lint-only` | 语法 + 类型 + 常见 RTL 陷阱 | matmul 的 testbench（`tb_main.sv` + `sources.f`）有 lint 警告升级为错误 |
| `shell` | Bash | `shellcheck` | 常见 shell 陷阱（未引用、SC2xxx） | 某 `.sh` 脚本有 shellcheck 报错 |

一个重要观察：**这四个检查的「严格度」 deliberately 不一样**。Python 检查只做 `compile()`（最宽松，只抓语法错误），不引入 `mypy`/`ruff`；SystemVerilog 检查只 lint matmul 的 testbench，不 lint 整个 `rtl/`；Shell 检查是唯一全仓扫描的。这种「因语言制宜」的严格度，是项目在「门禁价值」与「误报成本」之间的权衡——下一节源码精读会逐一看清。

#### 4.2.2 核心流程

四个检查的通用执行模型（以 `nix` 检查为例）：

```
runCommand "llm2fpga-nix" {
    nativeBuildInputs = [ statix deadnix nixfmt-classic ];   # 钉死工具版本
} ''
    cd ${./.}          # 进入仓库根（store 路径）
    deadnix --fail .   # 找未使用的 let 绑定，--fail 表示发现即失败
    statix check .     # 找 Nix 反模式（如不必要的 rec）
    nixfmt --check .   # 检查格式（不修改，只校验）
    mkdir -p "$out"    # 全过才产 $out → 派生成功
''
```

注意三个细节：

1. `cd ${./.}`——`./.` 是 Nix 里「当前文件（flake.nix）所在目录」的写法，会被求值成一个 store 路径。`cd` 进去后，命令针对的是**已被 Nix 复制进 store 的仓库快照**，不是你的工作区——所以检查的是提交进 git 的内容。
2. 工具通过 `nativeBuildInputs` 进入 PATH，**版本由 flake 钉死**（`pkgs.statix` 等是 nixpkgs-24.05 里的具体版本）。
3. 命令用 `&&` 串联（这里靠 shell 的 errexit 隐式串联：`runCommand` 默认不开 `set -e`，但 `deadnix --fail` 发现问题会非零退出，下一条命令仍会执行——所以严格说要靠每条都成功才能走到 `mkdir`。下面源码精读会看到各检查的实际写法）。

实际上四个检查的串联方式略有差异，下面逐一拆。

#### 4.2.3 源码精读

**检查 1：`nix`（Nix 三连）**

[flake.nix:808-817](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L808-L817) —— 这是四个检查里工具最多的一条。逐行看：

- L809-810：`nativeBuildInputs = [ pkgs.statix pkgs.deadnix pkgs.nixfmt-classic ]`——三个 Nix 专用 linter，版本由 nixpkgs 钉死。注意是 `nixfmt-classic`（旧版 nixfmt 的稳定接口），与 L790 的 `formatter = pkgs.nixfmt-classic;` 以及 devShell 里的 `pkgs.nixfmt-classic`（L778）保持一致——**格式化器与检查器用同一个包**，保证「`nix fmt` 改完的格式」与「`nixfmt --check` 校验的格式」是同一套规则。
- L813：`deadnix --fail .`——`deadnix` 找未使用的 `let` 绑定、函数参数等死代码；`--fail` 让它在发现任何死代码时非零退出。
- L814：`statix check .`——`statix` 找 Nix 反模式（如不必要的 `rec`、`with` 滥用、可简化的写法）。
- L815：`nixfmt --check .`——只校验格式，**不修改**文件（要修改用 `nix fmt`）。`--check` 模式下格式不符会非零退出。
- L816：`mkdir -p "$out"`——三连全过才走到这里，产 `$out` 凭证。

**检查 2：`python`（仅 byte-compile）**

[flake.nix:819-832](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L819-L832) —— 这是最「轻」的检查。核心是内嵌的 Python 脚本：

- L820：`nativeBuildInputs = [ pkgs.python311 ]`——注意这里用的是**通用 `pkgs.python311`**（nixpkgs-24.05 的 Python 3.11），不是降级链用的 `pkgsLlvm21.python311`。因为这里只做语法 byte-compile，Python 版本差异对语法检查影响很小，没必要拖入 LLVM 包集。
- L823-830：内嵌 Python 用 `pathlib.Path(".").rglob("*.py")` 递归找全仓所有 `.py`，对每个文件 `compile(source, str(path), "exec")`。`compile()` 是 Python 内置函数，只做**词法 + 语法分析**生成 bytecode，**不执行**、也不做类型检查。
- L831：`mkdir -p "$out"`。

这条检查的定位很清楚：**只兜底「语法错误」**。它不抓未使用变量、不抓类型错误、不抓风格问题——项目刻意没在这里引入 `ruff`/`mypy`，避免对大量「写一次就跑」的脚本式 Python（如 `externalize_large_memories.py`）产生误报负担。一个 `.py` 能 `compile()` 过、能被降级链的 `runCommand` 实际跑起来，就算合格。

**检查 3：`systemverilog`（Verilator lint，只针对 matmul testbench）**

[flake.nix:834-849](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L834-L849) —— 这条检查最值得细看，因为它复用了降级链的产物：

- L835：`nativeBuildInputs = [ pkgs.verilator ]`——用 nixpkgs 的 verilator（注意不是 devShell 里那个？实际是同一个 `pkgs.verilator`）。
- L837-847：`verilator --lint-only --timing --language 1800-2017 --top-module tb --Wall --Wno-fatal --Wno-TIMESCALEMOD -I${tbDataSv} -f ${matmulSv}/sources.f ${./sim}/tb_main.sv`。逐个 flag 看：
  - `--lint-only`：只做语法/类型 lint，**不编译成可执行文件**（区别于 u4-l2 的 `--binary`）。
  - `--timing`：开启对 `#delay` 等时序语句的支持（u4-l2 讲过这是硬性开关）。
  - `--language 1800-2017`：按 SystemVerilog 2017 标准解析。
  - `--top-module tb`：顶层模块是 `tb`（即 testbench）。
  - `--Wall`：开所有 lint 警告。
  - `--Wno-fatal`：**警告不升级为 fatal**——即有警告不退出，只有真正的 error 才失败。这是「严查但不误杀」的取舍。
  - `--Wno-TIMESCALEMOD`：专门关掉 `TIMESCALEMOD` 这一条警告（timescale 修饰相关的噪声）。
  - `-I${tbDataSv}`：把 `tbDataSv` 派生的目录加进 include 路径——`tb_main.sv` 会 `\`include "tb_data.sv"`，这个文件由 `gen_tb_data.py` 在 u4-l1 生成（PyTorch 黄金参考）。**这条 check 因此依赖 `tbDataSv` 派生**——如果黄金参考生成挂了，lint 也会挂。
  - `-f ${matmulSv}/sources.f`：用降级链产出的文件清单（u3-l4 的 `export-split-verilog` 产出）读入所有 matmul RTL 文件。**这条 check 因此也依赖 `matmulSv` 派生**。
  - `${./sim}/tb_main.sv`：最后显式读入 testbench 本身。

这条检查的范围很重要：**它只 lint matmul 的 testbench 及其依赖的 matmul RTL**，不 lint `rtl/fp/circt_fp_primitives.sv`（浮点原语）、不 lint `fpga/rtl/matmul_selftest_top.sv`（自测外壳）、更不 lint TinyStories 相关文件。原因有二：一是 matmul 是冒烟核，保证它 lint 干净就能守住降级链产出的 SV 质量；二是 `circt_fp_primitives.sv` 用了大量定点近似写法，过 `--Wall` 会有一堆警告，强行 lint 会噪声淹没信号。

- L848：`mkdir -p "$out"`。

**检查 4：`shell`（全仓 shellcheck）**

[flake.nix:851-861](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L851-L861) —— 这是唯一**全仓扫描**的检查：

- L852：`nativeBuildInputs = [ pkgs.shellcheck pkgs.findutils ]`——`shellcheck` 是 bash 静态分析器，`findutils` 提供 `find`/`xargs`。
- L855-858：一个优雅的「空集短路」：`if ! find . -name '*.sh' -type f | grep -q .; then mkdir -p "$out"; exit 0; fi`——如果仓库里一个 `.sh` 都没有，直接产 `$out` 成功退出。这是一个**防御性写法**：因为下一行 `find ... | xargs -0 shellcheck` 在「无输入文件」时，`xargs` 默认会报错（exit 123），这个短路避免了「删光所有脚本反而 CI 红」的尴尬。
- L859：`find . -name '*.sh' -type f -print0 | xargs -0 shellcheck -s bash -x`——`-print0` / `-0` 用空字节分隔文件名（处理含空格/特殊字符的路径）；`-s bash` 指定方言为 bash（脚本可能用了 bashism）；`-x` 跟随 `source` 的脚本一并检查。这一条会扫到 `scripts/pipeline/*.sh`、`scripts/compile-pytorch.py`（不，那是 .py）、以及 `scripts/dev` 等所有 shell 脚本。
- L860：`mkdir -p "$out"`。

注意 `shellcheck` 默认把不少警告当 error（不像 verilator 的 `--Wno-fatal`）——所以这条检查最严格，`scripts/pipeline/common.sh` 等脚本必须写得非常干净。

#### 4.2.4 代码实践

**实践目标**：用「单跑 + 故意触发」的方式，建立「检查名 → 工具 → 失败信号」的完整映射。

**操作步骤**：

1. 先全量跑一遍，确认当前仓库是绿的（需 Nix）：
   ```bash
   nix build .#checks.x86_64-linux.nix --no-link && echo "nix OK"
   nix build .#checks.x86_64-linux.python --no-link && echo "python OK"
   nix build .#checks.x86_64-linux.systemverilog --no-link && echo "sv OK"
   nix build .#checks.x86_64-linux.shell --no-link && echo "shell OK"
   ```
2. 单独验证 SystemVerilog 检查的依赖链：读 [flake.nix:834-849](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L834-L849)，指出这条 lint 用到了哪两个**降级链产物**（提示：一个提供 include，一个提供 `sources.f`）。
3. 读 [flake.nix:855-858](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L855-L858)，解释为什么 shell 检查要先做「`find | grep -q .`」短路——预测：若去掉这个短路、且仓库里恰好没有 `.sh` 文件，`xargs` 会怎样？

**需要观察的现象 / 预期结果**：

- 步骤 1：四条命令都应打印 OK（在当前 HEAD，仓库是绿的）。
- 步骤 2：SystemVerilog lint 依赖 `tbDataSv`（提供 `tb_data.sv`，`-I${tbDataSv}`）与 `matmulSv`（提供 `sources.f` 与 matmul RTL，`-f ${matmulSv}/sources.f`）。这两个都是降级链的派生产物——这意味着 SystemVerilog lint 实际上会**先触发 matmul 的前端降级**（PyTorch→…→SV），Nix 会自动构建这些依赖。所以单跑 `systemverilog` check 比 `nix`/`python`/`shell` 慢得多。
- 步骤 3：去掉短路后，若 `find` 找不到任何 `.sh`，`xargs -0 shellcheck` 收到空输入，`xargs` 默认以非零退出码结束（GNU xargs 在无输入时 exit 123），导致 check 假阴性失败。这个短路就是为此而设。

> 注：步骤 1 需本地有 Nix 且能联网拉取/构建 verilator 等依赖。若环境受限，步骤 2、3 为纯源码阅读型，可直接读 flake.nix 完成推断。「待本地验证」步骤 1 的实际构建耗时。

#### 4.2.5 小练习与答案

**练习 1**：四个检查里，哪一个是「全仓扫描」，哪几个是「限定范围」？为什么这样设计？

**答案**：`shell` 是全仓扫描（`find . -name '*.sh'`）；其余三个都限定范围——`nix` 扫所有 `.nix`（仓库里 `.nix` 主要是 `flake.nix`、`nix/*.nix`、`torch-mlir.nix`，范围可控）；`python` 扫所有 `.py`（但只做最轻的 byte-compile）；`systemverilog` **只** lint matmul 的 testbench 及其依赖（见 [flake.nix:837-847](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L837-L847)）。SystemVerilog 范围最窄，是因为 `rtl/fp/circt_fp_primitives.sv` 含大量定点近似写法、过 `--Wall` 噪声大；只保 matmul 冒烟核的 SV 干净，就能守住降级链产出质量。

**练习 2**：Python 检查用的是 `pkgs.python311`（nixpkgs-24.05），而降级链的 `compile-pytorch.py` 实际跑在 `pkgsLlvm21.python311` 上。这个不一致有问题吗？

**答案**：对本检查的目标（byte-compile 语法检查）没有问题。`compile()` 只做词法/语法分析，Python 3.11 的语法在两个包集里是同一套（都是 3.11），所以语法错误在这边能被抓到。项目刻意用通用 `pkgs.python311` 而非 LLVM 包集的 Python，是为了避免把沉重的 LLVM 依赖拖进一个只做语法检查的派生——这能显著缩短 CI 时间。代价是不检查运行时行为，但那本来就靠真实 `runCommand`（如 u4-l1 的 `gen_tb_data.py`）兜底。

**练习 3**：SystemVerilog 检查用了 `--Wall`（全开警告）却又用 `--Wno-fatal`（警告不致命）。这两个 flag 一起用是什么效果？为什么不同时省掉它们？

**答案**：`--Wall` + `--Wno-fatal` 的组合是「**尽可能多地把潜在问题作为警告打印出来，但不让任何一条警告直接 fail 构建**」——只有真正的 error（如语法错误、类型错误）才让 check 失败，警告只进日志。这样既能看到所有可疑点（信息价值），又不被噪声误杀（CI 稳定性）。同时省掉的话就是默认严格度，会漏掉一些 `--Wall` 才覆盖的检查项；只开 `--Wall` 不加 `--Wno-fatal` 则会让定点近似等合法写法的警告直接挂 CI，噪声过大。当前组合是经过取舍的平衡点。

---

### 4.3 docs-md 文档生成：Org 为权威源，Markdown 为派生

#### 4.3.1 概念说明

仓库的文档有两种格式：`.org`（Emacs Org mode）和 `.md`（GitHub Flavored Markdown）。这两者的关系不是「并列」，而是「**权威源 vs 派生物**」：

- `.org` 是**权威源（canonical source）**——所有文档的修改都在 `.org` 上进行。
- `.md` 是**派生物**——由 `docs-md` 应用自动从 `.org` 生成，给不用 Org mode 的读者（以及 GitHub 网页直接预览）看。

这条约定在 [README.md:111-114](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L111-L114) 有一行人读版本：「The canonical sources are `.org` files. Markdown files are generated for readers who do not use Org mode.」

实现这条约定的工具是 `docs-md` 应用，它的本质是一个被 `writeShellApplication` 包好的 bash 脚本，内部调用 **pandoc** 把 Org 转成 GitHub Flavored Markdown（`-f org -t gfm`）。它被暴露为 flake 的 `apps.docs-md`，所以用 `nix run .#docs-md` 触发。

为什么这事重要？因为如果没有自动化，`.md` 很容易和 `.org` 脱节——作者改了 `.org` 忘了重新导出 `.md`，读者看到的 Markdown 就是旧的。`docs-md` 把「导出」变成一条可重复命令，并在 CI 之外提供随时重新生成的能力（注意：`docs-md` **不在 `checks` 里**，所以 CI 不强制校验 `.md` 是否最新——它是一个手动/按需工具）。

#### 4.3.2 核心流程

```
作者改 deliverables/3e-tiny-stories-1m-resource-report.org
   │
   ▼ （手动触发）
nix run .#docs-md
   │
   ▼ apps.docs-md → docsMdApp/bin/docs-md
   （writeShellApplication 包好的脚本，runtimeInputs=[pandoc, coreutils]）
   │
   ├─ 找到三类权威源：
   │    $repo/README.org
   │    $repo/deliverables/*.org
   │    $repo/docs/*.org
   │
   ├─ 对每个 .org：
   │    dst = 去掉 .org 后缀加 .md（如 3e-...org → 3e-...md）
   │    pandoc "$src" -f org -t gfm -o "$dst"
   │
   └─ 产物 .md 落在原目录（与 .org 同级），覆盖旧 .md
```

关键点：`docs-md` 只处理**三类路径**下的 `.org`：根目录的 `README.org`、`deliverables/` 下全部 `.org`、`docs/` 下全部 `.org`。它**不**递归扫描全仓库——这意味着如果你在 `rtl/` 或 `scripts/` 里放一个 `.org`，它不会被转成 `.md`。这个范围的选定是 deliberate 的，下一节解释为什么。

#### 4.3.3 源码精读

**先看 `docsMdApp` 的定义——整个生成器的核心**：

[flake.nix:608-627](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L608-L627) —— `docsMdApp = pkgs.writeShellApplication { ... }`。逐段看：

- L609：`name = "docs-md";`——这是最终可执行文件的名字（`$out/bin/docs-md`）。
- L610：`runtimeInputs = [ pkgs.pandoc pkgs.coreutils ];`——**pandoc** 做格式转换，**coreutils** 提供 `pwd`/`cd` 等基础命令。注意 `writeShellApplication` 会把这些工具加进脚本的 PATH，且版本被 flake 钉死——这意味着 `nix run .#docs-md` 用的 pandoc 版本，和 devShell 里、CI 里完全一致，不会因系统 pandoc 升级而输出变化。
- L611-626：脚本本体。逐行看：
  - L612：`set -euo pipefail`——严格模式：未定义变量即错、任一管道段失败即整体失败。这是稳健 shell 脚本的标准起手式（`shellcheck` 也会推荐）。
  - L614-618：定位仓库根目录。默认 `repo="$(pwd -P)"`（当前工作目录的物理路径，`-P` 解析符号链接）；若传了参数 `$1` 则用它；最后 `cd "$repo" && pwd -P` 验证并规范化。这让 `docs-md` 既能在仓库根跑，也能 `nix run .#docs-md -- /path/to/repo` 指定路径。
  - L620-624：转换循环。`while IFS= read -r src; do ... done < <(...)` 是 bash 的「进程替换」惯用法——把三个路径模式（`$repo/README.org`、`$repo/deliverables/*.org`、`$repo/docs/*.org`）用 `printf '%s\n'` 逐行喂给循环。
    - L621：`[ -f "$src" ] || continue`——**容错**：如果某条路径不存在（例如 `docs/*.org` 在某次重构后被删空），跳过而不报错。这正是 `docs-md` 只处理三类路径、但某类为空时也能跑的原因。
    - L622：`dst="${src%.org}.md"`——bash 参数展开，去掉 `.org` 后缀加 `.md`。
    - L623：`pandoc "$src" -f org -t gfm -o "$dst"`——从 Org（`-f org`）转成 GitHub Flavored Markdown（`-t gfm`），输出到 `$dst`（`-o`）。**产物落在原目录、覆盖旧 `.md`**。
  - L625：`echo "Generated markdown docs in: $repo"`——人读的确认信息。

**再看 `docs-md` 如何被暴露成 flake app**：

[flake.nix:800-805](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L800-L805) —— `apps.docs-md = { type = "app"; program = "${docsMdApp}/bin/docs-md"; };`。`writeShellApplication` 的产物是一个派生，其 `bin/docs-md` 是可执行文件；`apps.<name>.program` 指向它后，就能用 `nix run .#docs-md` 触发。注意它与 `packages`（L792-798）、`checks`（L807-862）是并列的 flake 输出——`apps` 是「命令」、`packages` 是「构建产物」、`checks` 是「门禁」，三者各司其职。

**最后看约定的口头说明**：

[README.md:111-118](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L111-L118) —— README 的「Documentation formats」一节，告诉读者：权威源是 `.org`，`.md` 是为不用 Org mode 的读者生成的，运行 `nix run .#docs-md` 重新生成。这是 `docsMdApp` 行为的人类可读镜像——脚本负责执行，README 负责让贡献者知道这条约定。

**为什么只处理三类路径？** 综合 [flake.nix:624](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L624) 与仓库结构，原因有三：

1. **这三类是「面向读者的正式文档」**：`README.org` 是项目门面，`deliverables/*.org` 是各任务（Task 1-3）的交付物（与 `project-plan_v2.md` 一一对应，见 u1-l2），`docs/*.org` 是项目计划。它们都是需要让外部读者（包括 GitHub 网页浏览者）看到的。
2. **避免误转内部笔记**：仓库里其它目录（如 `scripts/`、`rtl/`、`nix/`）若有零散 `.org` 笔记，是开发者私用、不应公开成 `.md`。限定三类路径就是把「公开文档」和「私有笔记」隔开。
3. **glob 模式简单可控**：`$repo/deliverables/*.org`、`$repo/docs/*.org` 是单层 glob（不递归），范围明确、无意外。若用 `find . -name '*.org'` 全仓扫，反而会把不该导出的东西卷进来。

这也解释了为什么 `docs-md` 不在 `checks` 里——它不校验「`.md` 是否最新」，只提供「重新生成」的能力。项目目前不在 CI 里强制 `.md` 与 `.org` 同步（那需要在 CI 里先跑 `docs-md`、再 `git diff` 检查，开销与收益不匹配）；贡献者需自觉在改 `.org` 后跑一次 `nix run .#docs-md` 并提交生成的 `.md`。

#### 4.3.4 代码实践

**实践目标**：亲手跑一次文档生成，看清「Org→Markdown」的转换范围与产物落点；并解释为什么只处理三类 `.org`。

**操作步骤**：

1. 在仓库根目录执行（需 Nix）：
   ```bash
   nix run .#docs-md
   ```
2. 观察输出末尾应打印 `Generated markdown docs in: <repo路径>`。
3. 选一个 deliverable 验证转换：用 `git status` 看 `deliverables/3e-tiny-stories-1m-resource-report.md` 是否被触碰（若 `.org` 自上次生成后未改，pandoc 重写内容相同、git 可能不报变化；可先在 `.org` 末尾加一行注释再跑，观察 `.md` 对应变化）。
4. 验证「只处理三类路径」：在 `scripts/` 目录下临时建一个 `scripts/_probe.org`（内容随意），再跑 `nix run .#docs-md`，观察 `scripts/_probe.md` **是否被生成**（预期：不生成，因为 `scripts/` 不在三类路径里）。**做完记得删除探针文件**（本讲禁止留改动）。
5. 读 [flake.nix:624](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L624)，确认那行 `printf` 喂给循环的正是三个模式：`$repo/README.org`、`$repo/deliverables/*.org`、`$repo/docs/*.org`。

**需要观察的现象 / 预期结果**：

- 步骤 2：打印确认信息，无报错。
- 步骤 3：`.md` 被（重）生成，内容是 `.org` 经 pandoc 转换后的 GFM，Org 特有语法（如 `#+TITLE`、`*` 标题层级）被映射成 Markdown 对应物。
- 步骤 4：`scripts/_probe.md` **不被生成**——证实 `docs-md` 的作用域严格限定在三类路径。`_probe.org` 探针用完后删除。
- 步骤 5：确认三个 glob 模式与「权威源三类」完全对应。

> 注：步骤 1-4 需本地有 Nix。步骤 5 为纯源码阅读型，可直接完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `docs-md` 用 `writeShellApplication` 而不是直接写进 `checks` 里当一个 `runCommand`？

**答案**：两者的用途不同。`writeShellApplication` 产出的是一个**可复用的命令**（暴露为 `apps.docs-md`，用 `nix run` 触发），目的是「按需重新生成文档」；`runCommand` 在 `checks` 里产出的是**门禁**（由 `nix flake check` 触发），目的是「失败即拦住 CI」。`docs-md` 的角色是工具（生成物），不是门禁（校验物），所以放在 `apps`。此外，`writeShellApplication` 自动生成带固定 `runtimeInputs` 的脚本，适合「一个会被反复手动调用的命令」；`runCommand` 更适合「一次性、产物驱动」的派生。

**练习 2**：如果有人在 `nix/` 目录下写了一个 `nix/notes.org`，`docs-md` 会把它转成 `.md` 吗？为什么？

**答案**：不会。[flake.nix:624](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L624) 只喂三类路径给转换循环：`README.org`、`deliverables/*.org`、`docs/*.org`。`nix/` 不在其中，所以 `nix/notes.org` 被忽略。这是 deliberate 的范围限定——只把「面向读者的正式文档」转成 Markdown，私有笔记不公开（见 4.3.3 的三条理由）。

**练习 3**：`docs-md` 不在 `checks` 里，意味着 CI 不会校验「`.md` 是否与 `.org` 同步」。如果你想让 CI 强制校验同步，思路是什么？会带来什么代价？

**答案**：思路是在 `checks` 里加一条派生：先 `nix run .#docs-md`（或直接调 pandoc）把所有 `.org` 转成 `.md` 到临时目录，再与仓库里 commit 的 `.md` 做 `diff`；若有差异（说明作者改了 `.org` 没重新导出），派生失败。代价有二：① CI 要多装 pandoc、多跑一轮转换，增加 CI 时间；② pandoc 不同版本的输出可能有微小差异（如空行、列表缩进），如果 CI 的 pandoc 版本与作者本地不一致，会产生「假 diff」导致 CI 假红——这正是本项目用 `writeShellApplication` 钉死 pandoc 版本要规避的问题，但即便如此，跨平台（行尾、locale）仍可能引入噪声。项目当前选择「不强制、靠自觉」，是收益与成本权衡的结果。

---

## 5. 综合实践

**任务**：把本讲三个最小模块（`nix flake check` 汇总、四语言 lint、`docs-md` 生成）串起来，完成一次「**CI 故障排查模拟**」——不实际改坏源码，而是读源码预测「如果某类问题发生，哪个 check 会先红、报什么」。

**目标**：建立「问题类型 → 对应 check → 失败信号」的完整映射表。

**操作步骤**：

1. **列出四个检查及其工具**。读 [flake.nix:807-862](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L807-L862)，把下表填满：

   | 检查名 | 工具 | 检查范围 | 触发命令 |
   | --- | --- | --- | --- |
   | `nix` | ？ | 全仓 `.nix` | ？ |
   | `python` | ？ | 全仓 `.py` | ？ |
   | `systemverilog` | ？ | ？ | `verilator --lint-only ...` |
   | `shell` | ？ | 全仓 `.sh` | ？ |

2. **故障映射预测**。对下面每个「假设的改动」，预测哪个 check 会失败（或都不失败）：

   | 假设的改动 | 哪个 check 红？ | 为什么？ |
   | --- | --- | --- |
   | a. `flake.nix` 里留了一个未使用的 `let` 绑定 | ？ | ？ |
   | b. `scripts/pipeline/common.sh` 里有一处未引用的 `$var` | ？ | ？ |
   | c. `src/matmul_adapter.py` 缺了一个冒号（语法错误） | ？ | ？ |
   | d. 改了 `deliverables/3e-...org` 但忘了跑 `docs-md` | ？ | ？ |
   | e. `sim/tb_main.sv` 里写了一个未声明的信号 | ？ | ？ |

3. **解释 `docs-md` 的范围**。读 [flake.nix:624](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L624)，说明为什么 `docs-md` 只处理 `README.org` + `deliverables/*.org` + `docs/*.org`，而不是 `find . -name '*.org'` 全仓扫。

**预期结果**：

- 步骤 1 表格：
  - `nix`：工具 `deadnix` + `statix` + `nixfmt`；命令 `deadnix --fail . && statix check . && nixfmt --check .`。
  - `python`：工具 `python311` 内置 `compile()`；命令是内嵌 Python 脚本 `rglob("*.py")` + `compile(...)`。
  - `systemverilog`：工具 `verilator`；范围**仅 matmul testbench 及其依赖**（`tb_main.sv` + `matmulSv/sources.f` + `tbDataSv`）。
  - `shell`：工具 `shellcheck`；命令 `find . -name '*.sh' -print0 | xargs -0 shellcheck -s bash -x`。
- 步骤 2 故障映射：
  - a → `nix` 红（`deadnix --fail` 发现未使用绑定）。
  - b → `shell` 红（shellcheck 报未引用变量，SC2086 之类）。
  - c → `python` 红（`compile()` 抛 `SyntaxError`，脚本异常退出，派生失败）。
  - d → **都不红**（`docs-md` 不在 `checks` 里，CI 不校验 `.md` 同步；只是 `.md` 会过时，直到有人手动跑 `nix run .#docs-md`）。这是「陷阱题」，用来检验你是否理解 `docs-md` 与 `checks` 的分离。
  - e → `systemverilog` 红（verilator lint 报未声明信号，这是 error 级，不受 `--Wno-fatal` 影响）。
- 步骤 3：三类路径是「面向读者的正式文档」（README 是门面、deliverables 是任务交付物、docs 是项目计划）；全仓 `find` 会把 `scripts/`、`rtl/`、`nix/` 下的私有 `.org` 笔记也卷进来误转；单层 glob 范围明确可控。

**自检清单**：

1. 你能否说出 `ci.yml` 的三个 step，且解释为什么核心 step 只有一行 `nix flake check`？（4.1）
2. 你能否区分四个 check 的严格度差异（Python 最轻、SystemVerilog 范围最窄、Shell 全仓最严）？（4.2）
3. 你能否解释 `docs-md` 为何是 `apps` 而非 `checks`、为何只处理三类路径？（4.3）

> 注：本任务为源码阅读 + 推断型，无需实际改坏源码。若想本地实证某条故障映射，可在 `nix develop` 里单跑对应 check（如 `nix build .#checks.x86_64-linux.shell`）观察报错——但**不要提交任何用于测试的破坏性改动**（本讲禁止改源码）。

## 6. 本讲小结

- **CI 极薄、flake 极厚**：[.github/workflows/ci.yml](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/.github/workflows/ci.yml) 只做 checkout → install nix → `LC_ALL=C nix flake check` 三步，所有具体检查都封在 `flake.nix` 的 `checks` 里；新增检查只改 flake、不动 CI，本地与 CI 同源。
- **`nix flake check` 的本质是「枚举并构建 `outputs.checks` 的每个派生」**：质量门禁在 Nix 世界里就是普通派生，`runCommand` 把任意 lint 命令包成派生，命令失败即派生失败即 CI 红；每个 check 末尾的 `mkdir -p "$out"` 是「检查通过」的物理凭证。
- **四个 check 因语言制宜、严格度不同**：`nix`（deadnix+statix+nixfmt，全仓 `.nix`）、`python`（仅 `compile()` byte-compile，最轻）、`systemverilog`（verilator lint，**只**针对 matmul testbench 及其降级链产物依赖）、`shell`（shellcheck，唯一全仓扫描）。
- **SystemVerilog check 复用降级链产物**：它依赖 `tbDataSv`（黄金参考 `tb_data.sv`）与 `matmulSv`（`sources.f`），因此单跑会先触发 matmul 前端降级——这是 CI 与降级链的隐式耦合点。
- **`docs-md` 是 `apps` 不是 `checks`**：它是「按需重新生成文档」的工具而非门禁；用 `writeShellApplication` 钉死 pandoc 版本，只把 `README.org` + `deliverables/*.org` + `docs/*.org` 三类**面向读者的正式文档**转成 GFM Markdown，私有 `.org` 笔记不卷入。
- **`.org` 为权威源、`.md` 为派生物**：`.md` 永不手改（README L111-114 的约定），改完 `.org` 要自觉跑 `nix run .#docs-md` 重新生成并提交。

## 7. 下一步学习建议

- **想看清 `runCommand` 的更多用法？** 回看 [u3-l5](u3-l5-pipeline-nix-orchestration.md)，那里讲 `runCommand` 如何把降级链的 9 个 shell 脚本包成可缓存派生——本讲的 `checks` 是同一机制的「门禁」用途，u3-l5 是「流水线」用途，对比阅读能加深理解。
- **想理解 SystemVerilog check 依赖的 `tbDataSv` 与 `matmulSv` 怎么来的？** 看 [u4-l1](u4-l1-golden-reference-and-vectors.md)（黄金参考生成 `tb_data.sv`）和 [u3-l4](u3-l4-hw-to-systemverilog-export.md)（`export-split-verilog` 产出 `sources.f`），本讲的 SV lint 正是建立在这两个产物之上。
- **想给项目加一个新 check？** 仿照 [flake.nix:808-817](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L808-L817) 的 `nix` 检查模板，写一个 `runCommand` 派生放进 `checks` attrset 即可，CI 自动覆盖——这是「薄 CI、厚 flake」最直接的二次开发入口。
- **最后一讲 [u7-l3](u7-l3-roadmap-and-resource-optimization.md)** 讲项目后续路线（Task 4/5/6 与资源优化方向），收束整套学习手册；本讲的质量门禁是「保证后续开发不退步」的工程基座，读完 u7-l3 你会对「为什么这套 CI 够用、还缺什么」有更完整的判断。
