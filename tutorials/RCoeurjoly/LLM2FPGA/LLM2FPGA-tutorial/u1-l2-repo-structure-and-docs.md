# 仓库结构与文档体系

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 LLM2FPGA 仓库里每个顶层目录（`src/`、`scripts/`、`rtl/`、`fpga/`、`sim/`、`nix/`、`patches/`、`deliverables/`、`docs/` 等）各自负责什么。
- 把每个目录对应到上一讲学过的「PyTorch → torch-MLIR → CIRCT → SystemVerilog → Yosys」降级链的哪一段。
- 理解为什么这个仓库把 `.org` 文件当作「权威源（canonical source）」，而 `.md` 是由命令自动生成的，并知道怎么重新生成它。
- 读懂 `deliverables/` 和 `docs/` 里那种 `<任务号><子任务>-<主题>` 的文件命名规则，看到一个文件名就能反推出它属于哪个任务。

一句话：本讲给你一张「仓库地图」，让你之后读任何一篇源码讲义时，都能迅速定位它在整个工程里的位置。

## 2. 前置知识

本讲承接 [u1-l1](u1-l1-project-overview.md)，那里已经讲清了项目要走的总路线。在继续之前，先确认这几个概念：

- **降级（lowering）**：把一种较高层的表示（比如 PyTorch 的张量运算）一步步翻译成更底层、更接近硬件的表示（最终是 RTL/Verilog）。LLM2FPGA 的整个仓库，基本就是围绕一条这样的降级链组织的。
- **目录树（directory tree）**：用缩进表示文件夹与文件之间的包含关系。本讲会频繁画这种树。
- **git submodule（子模块）**：把另一个独立的 git 仓库「挂」进当前仓库的某个子目录里。它有自己的 `.git`，版本被单独固定。本仓库用 `.gitmodules` 文件登记有哪些子模块。
- **Org 文件（`.org`）**：Emacs 编辑器的 Org mode 使用的纯文本格式，本质是带轻量标记的文本（用 `*` 表示标题、`-` 表示列表等），非常适合写带任务状态的笔记和计划。
- **flake（Nix flake）**：Nix 包管理器的项目描述文件。本仓库根目录的 `flake.nix` 既描述工具链，也定义了一些「命令式入口」（叫 app），其中 `docs-md` 就是用来生成 Markdown 文档的。Nix 本身在 [u1-l3](u1-l3-nix-reproducible-toolchain.md) 会专门讲，这里你只要知道 `nix run .#docs-md` 是一条「跑某个 app」的命令即可。

> 不熟悉这些名词没关系，下面用到时都会再解释一遍。

## 3. 本讲源码地图

本讲主要读这几份文件，它们都属于「项目元信息」而非可执行逻辑：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md) | 仓库门面：项目定位、如何复现、读哪些文档、文档格式约定 |
| [.gitmodules](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/.gitmodules) | 登记本仓库的 git submodule |
| [docs/project-plan_v2.md](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/docs/project-plan_v2.md) | 项目计划，定义了 Task 1–6 的任务编号体系，是 `deliverables/` 命名的依据 |
| [flake.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix) | 其中的 `docs-md` app 是「`.org` → `.md`」生成机制的真实实现 |

---

## 4. 核心概念与源码讲解

### 4.1 目录职责速览

#### 4.1.1 概念说明

LLM2FPGA 不是一坨散乱的脚本，而是一条**有序的降级流水线**。仓库的目录划分，几乎就是这条流水线的「站段划分」——每个目录对应流水线的一个（或几个）阶段。因此，记住上一讲那张总路线，就能反过来猜出每个目录是干什么的：

```
PyTorch 模型  →  torch-MLIR  →  CIRCT(dialect 降级)  →  SystemVerilog  →  Yosys(综合/资源)
```

下面这张表是本讲最重要的「地图」，建议背下来。表中的「代表性文件」都是仓库里真实存在的（可对照 [README.md:56-65](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L56-L65) 给出的「Start here」入口）。

| 目录 | 内容 / 代表性文件 | 流水线位置 |
|------|------|------|
| `src/` | `matmul.py`、`matmul_adapter.py` | **PyTorch 前端**：最小核（matmul）与它的适配器 |
| `TinyStories/` | `model_adapter.py` | **PyTorch 前端**：真实 LLM（TinyStories-1M）的适配器 |
| `scripts/` | `compile-pytorch.py` + `pipeline/` 子目录 | **前端导出 + 整条降级脚本链** |
| `scripts/pipeline/` | `torch_to_linalg.sh`、`cf_to_handshake.sh`、`hw_clean_to_sv.sh`、`sv_to_il.sh` 等 | **torch-MLIR / CIRCT / Yosys 降级**：每脚本包一站 |
| `rtl/` | `fp/circt_fp_primitives.sv`、`tiny_stories_selftest_top.sv` | **生成的 SV 的「伴随手写支撑」**：浮点原语、自测外壳 |
| `fpga/` | `fpga/rtl/matmul_selftest_top.sv`、`fpga/constraints/*.xdc` | **板级集成**：自测顶层 + 引脚/时序约束 |
| `sim/` | `tb_main.sv`、`gen_tb_data.py`、`test_vectors.json` | **仿真与黄金参考**：用 PyTorch 当真相源验证 RTL |
| `nix/` | `pipeline.nix`、`models.nix` | **构建编排**：把降级脚本串成可缓存的派生链 |
| `patches/` | `circt-task3-rfp/*.patch` | **工具链补丁**：让上游 CIRCT 能跑通 TinyStories 降级 |
| `deliverables/` | `1a-survey.org`…`3e-…org` 及对应 `.md` | **任务交付文档**（按任务编号命名）|
| `docs/` | `project-plan_v2.org`、`project-management.org` | **项目计划与管理** |
| `LLM2FPGA-genAI-logs/` | （git submodule） | **AI 辅助开发日志**，单独成仓 |

> 几个容易踩坑的点：
>
> 1. **入口 `flake.nix` 在仓库根目录**，不在 `nix/` 里。`nix/` 只放被 `flake.nix` 调用的模块化 nix 文件（`pipeline.nix`、`models.nix`、`nanobind-bootstrap.nix`）；此外根目录还有一个 `torch-mlir.nix`（独立 pin 的 torch-MLIR 构建）。
> 2. **`rtl/` 和 `fpga/rtl/` 都有 `.sv`，但含义不同**：`rtl/` 放的是「降级产出的 SV 需要依赖的手写支撑」（比如浮点原语实现）；`fpga/` 放的是「为了上板而加的胶水逻辑和约束」。前者服务降级链，后者服务硬件集成。
> 3. **`scripts/` 一肩挑两段**：`compile-pytorch.py` 负责前端导出（torch.export + torch-MLIR），`scripts/pipeline/` 里的 shell 脚本负责后面所有 CIRCT/Yosys 降级。

#### 4.1.2 核心流程

把目录「翻译」成流水线站段，可以画成下面这条对应关系（从上到下就是数据流向）：

```
src/matmul.py ──┐                          （PyTorch 模型本体）
TinyStories/    ├──> scripts/compile-pytorch.py   （前端导出为 MLIR）
                       │
                       v
            scripts/pipeline/*.sh              （torch-MLIR → CIRCT → SV → Yosys）
              │   │   │   │
   torch_to_  │   │   │   └─ sv_to_il.sh         （Yosys 读 SV 出 RTLIL）
   linalg     │   │   └─ hw_clean_to_sv.sh       （HW → SystemVerilog）
              │   └─ cf_to_handshake.sh 等       （CF → Handshake → HW）
              └─ （依赖 patches/circt-task3-rfp/ 修复上游工具）
                       │
   rtl/fp/*.sv ────────┤                        （给降级出的浮点 extern 提供实现）
                       v
            nix/pipeline.nix                     （把上面串成可缓存派生）
                       │
        ┌──────────────┴───────────────┐
        v                              v
   sim/ (Verilator 验证)         fpga/ (上板自测外壳 + 约束)
```

一句话流程：**模型在前端目录（`src/`、`TinyStories/`）被定义，经 `scripts/` 降级成硬件描述（`rtl/` 配合），由 `nix/` 编排缓存，最终用 `sim/` 验证语义、用 `fpga/` 准备上板**；`patches/` 在降级链中途修补上游工具；`deliverables/` 和 `docs/` 记录每一步的结论。

#### 4.1.3 源码精读

我们用仓库根目录的「实际顶层目录列表」来印证上面这张表（以下目录均为真实存在）：

```
src/  TinyStories/  scripts/  rtl/  fpga/  sim/  nix/  patches/
deliverables/  docs/  LLM2FPGA-genAI-logs/  README.*  flake.nix  flake.lock  torch-mlir.nix  LICENSE
```

其中 `LLM2FPGA-genAI-logs/` 是一个 git submodule，它的登记信息在 [.gitmodules:1-3](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/.gitmodules#L1-L3)：

```ini
[submodule "LLM2FPGA-genAI-logs"]
    path = LLM2FPGA-genAI-logs
    url = git@github.com:RCoeurjoly/LLM2FPGA-genAI-logs.git
```

这几行说明：`LLM2FPGA-genAI-logs/` 这个子目录其实指向**另一个独立仓库**（记录 AI 辅助开发过程的日志），它不参与编译流水线，只是归档用途。这也解释了为什么它在 `git ls-files` 里只显示为一个条目，而看不到里面的具体文件——子模块内容由它自己的仓库管理。

README 的「Start here」一节（[README.md:56-65](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L56-L65)）进一步印证了文档目录的分工：它建议读者先读 `deliverables/` 下的三份关键交付物，再读 `docs/` 下的项目计划——也就是「`deliverables/` = 各任务结论，`docs/` = 全局计划」。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（不需要运行任何命令，目标是练出「看文件名就知站段」的直觉）。

1. **实践目标**：仅凭文件名，判断脚本属于降级链的哪一站。
2. **操作步骤**：
   - 打开 `scripts/pipeline/` 目录，列出所有 `.sh` 文件。
   - 对照 [4.1.1](#411-概念说明) 的表，给每个文件标注它处于 `torch → linalg → cf → handshake → hw → sv → il` 中的哪一段。
3. **需要观察的现象**：你会发现这些脚本名几乎「自带顺序」，例如 `torch_to_linalg.sh`（torch 方言到 Linalg）、`linalg_to_cf.sh`（Linalg 到控制流）、`cf_to_handshake.sh`（控制流到 Handshake）、`hw_clean_to_sv.sh`（HW 到 SystemVerilog）、`sv_to_il.sh`（SystemVerilog 到 RTLIL）。
4. **预期结果**：你能按文件名排出一条与上一讲总路线完全吻合的「脚本子链」，并理解 `common.sh`、`yosys_common.sh`、`write_utilization_report.py` 这类「工具型」文件不属于任何具体站段，而是被各站段共享的脚手架。
5. 如果某个文件名你无法归位，标注「待确认」，留到 [u2/u3](u2-l1-models-and-adapters.md) 系列讲义里核实。

#### 4.1.5 小练习与答案

**练习 1**：`scripts/compile-pytorch.py` 和 `scripts/pipeline/torch_to_linalg.sh` 都涉及「torch」，它们职责有何不同？

> **参考答案**：`compile-pytorch.py` 在**前端**，负责把 PyTorch 模型导出成 torch 方言 MLIR 文本（用 `torch.export` + `torch_mlir`）；`torch_to_linalg.sh` 在**降级链**，用 `torch-mlir-opt` 把那段 torch 方言进一步降级到 Linalg-on-Tensors。前者是「模型 → MLIR」，后者是「torch 方言 → Linalg 方言」。

**练习 2**：`rtl/` 和 `fpga/rtl/` 里都有 `.sv` 文件，为什么仓库要把它们分开放？

> **参考答案**：`rtl/` 放的是降级产出的 SystemVerilog 所**依赖**的手写支撑（典型例子 `rtl/fp/circt_fp_primitives.sv`，为浮点 extern 提供近似实现），它服务于降级链本身；`fpga/` 放的是为**实际上板**而加的胶水顶层和约束文件，服务于硬件集成阶段。两者目的不同，所以分开。

---

### 4.2 Org 权威源与 Markdown 生成

#### 4.2.1 概念说明

进到 `deliverables/` 或 `docs/` 你会发现一个现象：**几乎每份文档都有成对的 `.org` 和 `.md` 两个文件**（例如 `1a-survey.org` 与 `1a-survey.md`）。这不是重复劳动，而是一个刻意的约定：

- **`.org` 是权威源（canonical source）**：所有人工撰写、修改文档的工作，都只改 `.org`。
- **`.md` 是自动生成的**：给不用 Org mode 的读者看的，由一条命令从 `.org` 转换而来。

为什么要这么设计？因为 Org mode 能表达任务状态（`TODO`/`DONE`/`WAIT`）、能折叠、能内嵌代码块执行，非常适合做项目计划与带状态的研究笔记；但 GitHub 和大多数读者更习惯看 Markdown。于是项目用「单一权威源 + 自动生成」的方式兼顾两边，并避免两份文档各自修改后产生不一致。

#### 4.2.2 核心流程

文档生成是一条非常简单的单步流程：

```
*.org  ──(pandoc: org → gfm)──>  同名 .md
```

具体规则（来自 `docs-md` app 的实现，见下）：

1. 固定扫描三类输入：仓库根的 `README.org`、`deliverables/*.org`、`docs/*.org`。
2. 对每个 `.org`，把扩展名 `.org` 替换成 `.md` 作为输出路径。
3. 调用 `pandoc` 把 Org 格式转成 GitHub Flavored Markdown（`-f org -t gfm`）。

> 关键推论：**永远不要手改 `.md`**——下次重新生成会把你的修改覆盖掉。要改文档就改 `.org`，然后重新跑生成命令。README 里也是这么说的（[README.md:111-118](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L111-L118)）。

#### 4.2.3 源码精读

README 的「Documentation formats」一节明确写出了这个约定和重新生成的命令：

> The canonical sources are `.org` files. Markdown files are generated for readers who do not use Org mode.（[README.md:111-118](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L111-L118)）
>
> ```bash
> nix run .#docs-md
> ```

这条 `nix run .#docs-md` 调用的，就是 `flake.nix` 里名为 `docs-md` 的 app。它的实现是一个 shell 脚本，核心就这几行（[flake.nix:608-627](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L608-L627)）：

```bash
while IFS= read -r src; do
    [ -f "$src" ] || continue
    dst="${src%.org}.md"          # 把扩展名 .org 换成 .md
    pandoc "$src" -f org -t gfm -o "$dst"
done < <(printf '%s\n' "$repo/README.org" "$repo"/deliverables/*.org "$repo"/docs/*.org)
```

这段代码说明了几件事：

- **扫描范围写死**：输入只有 `README.org` + `deliverables/*.org` + `docs/*.org` 三类（第 624 行）。`nix/`、`scripts/`、`sim/` 等目录里的文档**不会**被转换。
- **输出就地生成**：`.md` 与 `.org` 同目录同名，只是扩展名不同（`${src%.org}.md`）。
- **依赖只有 `pandoc` 和 `coreutils`**（[flake.nix:609-610](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L609-L610)），这是个极轻的转换。

> 顺带一提：README 里那些「阅读入口」链接（如 `1a-survey.org`）一律指向 `.org` 而不是 `.md`（见 [README.md:58-65](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L58-L65)），再次说明项目把 `.org` 当作「要长期维护的那一份」。

#### 4.2.4 代码实践

这是一个**可运行的轻量实践**（前提是你已按 [u1-l3](u1-l3-nix-reproducible-toolchain.md) 进入或具备 Nix 环境；若无 Nix，可降级为纯阅读实践）。

1. **实践目标**：亲眼看到「`.md` 由 `.org` 生成」这件事，并理解改 `.md` 是徒劳的。
2. **操作步骤**：
   - 记下某份 `.md`（比如 `deliverables/1a-survey.md`）第一段的内容。
   - 手动在该 `.md` 文件末尾加一行 `<!-- 我手改的标记 -->`，保存。
   - 运行 `nix run .#docs-md`（无 Nix 时可改用 `pandoc deliverables/1a-survey.org -f org -t gfm -o deliverables/1a-survey.md`）。
   - 再次查看该 `.md`。
3. **需要观察的现象**：重新生成后，你手加的那行标记消失了，内容回到了 `.org` 的状态。
4. **预期结果**：得到结论——「`.md` 是产物，不是源；要改文档必须改 `.org`」。
5. 如果你无法运行 Nix/pandoc，明确写「待本地验证」，但仍可通过对比某对 `.org`/`.md` 的内容，确认二者只是格式差异、语义一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `docs-md` 的扫描范围只有 `README.org`、`deliverables/*.org`、`docs/*.org`，而不包含 `nix/` 或 `scripts/` 里的说明？

> **参考答案**：因为 `docs-md` 的职责是「生成给人读的项目文档」，而这些正式文档只放在根、`deliverables/`、`docs/` 三处。`nix/`、`scripts/` 里的注释是给开发者看源码时用的「就近文档」，不属于对外交付物，所以不纳入转换范围——也避免把临时脚本说明误当成正式文档。

**练习 2**：如果有人直接修改了 `deliverables/3e-...resource-report.md`，下次跑 `docs-md` 会怎样？

> **参考答案**：他的修改会被覆盖。因为 `docs-md` 会用 `pandoc` 从 `.org` 重新生成同名 `.md`，直接覆盖目标文件。正确的做法是改 `.org`。

---

### 4.3 deliverables/docs 的任务编号体系

#### 4.3.1 概念说明

`deliverables/` 里的文件名看起来很规整：`1a-`、`1b-`、`1c-`、`2a-`…`2e-`、`3a-`…`3e-`。这不是随手起的，而是和 `docs/project-plan_v2.md` 里定义的**任务编号体系**一一对应。

命名规则是：

```
<任务号><子任务字母>-<主题slug>.org   （外加同名 .md）
```

例如 `3e-tiny-stories-1m-resource-report.org` 表示「**Task 3 的子任务 e**，主题是 TinyStories-1M 的资源报告」。掌握了这个规则，看到文件名就能反推出它属于哪一阶段、回答什么问题。此外，个别任务还有同名子目录（如 `deliverables/1b/`、`deliverables/2b/`），用来存放日志、截图等附件。

#### 4.3.2 核心流程

任务编号与项目计划里的 Task 一一对应。`docs/project-plan_v2.md` 把整个项目规划成 6 个大任务（Task 1–6），每个任务再拆成若干子任务（a、b、c…）。简化对照如下：

| 任务号 | 任务主题（来自 `project-plan_v2.md`） | 对应的 `deliverables/` 文件 |
|--------|----------------------------------------|------------------------------|
| Task 1 | 调研与候选路线选择 | `1a-survey`、`1b-compatibility_check`、`1c-selected_route` |
| Task 2 | matmul 端到端语义等价 | `2a` ~ `2e`（nix flake、PyTorch↔SV 等价、Yosys RTLIL、比特流、报告）|
| Task 3 | 把小 LLM（TinyStories-1M）降级到 RTL | `3a` ~ `3e`（模型、pre-CIRCT MLIR、SV、RTLIL、资源报告）|
| Task 4 | FPGA 集成与硬件验证 | （尚无交付物，对应计划中的 4a–4e）|
| Task 5 | TinyStories 家族 scaling 分析 | （尚无交付物）|
| Task 6 | 资源用量削减策略 | （尚无交付物，是当前推进方向）|

> 也就是说：**`deliverables/` 目前主要装着 Task 1–3 的产出**；Task 4–6 在计划文档里已定义，但还没有对应的交付文件。这也和 [u1-l1](u1-l1-project-overview.md) 的结论一致——项目目前正从 Task 3（证明能降级但超配 141 倍）迈向 Task 6（削减资源）。

#### 4.3.3 源码精读

`docs/project-plan_v2.md` 的章节标题本身就列出了六大任务，比如：

- Task 1（已完成）：`## ... 1) Survey & candidate selection`
- Task 3（当前焦点）：`## TODO 3) Lowering of small LLM to RTL [0/5]`（[project-plan_v2.md:177](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/docs/project-plan_v2.md#L177)）
- Task 6（下一步方向）：`## ... 6) Resource usage reduction strategies [0/3]`（[project-plan_v2.md:440](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/docs/project-plan_v2.md#L440)）

Task 3 内部进一步细分子任务 a–e，其「Deliverables」清单（节选）正好对应 `deliverables/` 里的文件名：

```
- 3a) TinyStories-1M PyTorch 模型
- 3b) tiny_stories1m.mlir, 进入 CIRCT 之前
- 3c) tiny_stories1m.sv, 由 CIRCT 生成
- 3d) tiny_stories1m.il, 由 yosys 生成
- 3e) 综合资源报告 + 瓶颈报告
```

这就是为什么仓库里有 `3a-tiny-stories-1m-pytorch-model.md`、`3b-tiny-stories-1m-pre-circt-mlir.md`、`3c-tiny-stories-1m-systemverilog.md`、`3d-tiny-stories-1m-rtlil.md`、`3e-tiny-stories-1m-resource-report.md` 这一串——**文件名 = 子任务编号 + 该子任务产出物的主题**。这套命名让你不用打开文件，就能知道它记录的是降级链的哪一段。

另外，README 复现命令的目标名也遵循同源逻辑（[README.md:83-91](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/README.md#L83-L91)）：`tiny-stories-1m-baseline-float-selftest-all-memory-utilization` 这串由若干「开关段」拼成，每段描述流水线的一个变体选择——这和 `deliverables/` 的命名是两种不同的「复合命名」，但思路一致：**用结构化命名传达工程语义**。

#### 4.3.4 代码实践

这是一个**纯阅读型实践**，目标是熟练「文件名 ↔ 任务」的反查。

1. **实践目标**：任意给一个 `deliverables/` 文件名，能说出它属于哪个 Task、回答什么问题。
2. **操作步骤**：
   - 列出 `deliverables/` 下所有文件（你会看到 13 对 `.org`/`.md` 加两个子目录）。
   - 打开 `docs/project-plan_v2.md`，定位 Task 2 的「Deliverables」清单（约在 [project-plan_v2.md:166-176](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/docs/project-plan_v2.md#L166-L176) 附近）。
   - 把清单里 `2a)`~`2e)` 与 `deliverables/2a-*.md`~`2e-*.md` 一一对应。
3. **需要观察的现象**：计划文档里的「Deliverables」条目和仓库里实际存在的交付文件能对上号。
4. **预期结果**：你能填出一张「`2a` = nix flake 与 matmul.sv、`2b` = PyTorch↔SV 等价、`2c` = Yosys IL 与 stat、`2d` = FPGA 比特流、`2e` = 汇总报告」的对照表，并注意到 `deliverables/1b/`、`deliverables/2b/` 子目录里装的是支撑材料（如 `.log`、`.sh`、截图）。
5. 若某条计划里的 deliverable 在仓库中暂未找到对应文件，标注「待交付」。

#### 4.3.5 小练习与答案

**练习 1**：看到文件名 `3c-tiny-stories-1m-systemverilog.md`，请推断它属于哪个任务、讲的是什么。

> **参考答案**：`3c` 表示 **Task 3 的子任务 c**。Task 3 是「把 TinyStories-1M 降级到 RTL」，子任务 c 的产出是「由 CIRCT 生成的 SystemVerilog（`tiny_stories1m.sv`）」。所以这份文件记录的是降级链跑到「HW → SystemVerilog」之后的 RTL 产出与结构健全性检查。

**练习 2**：`deliverables/1b-compatibility_check` 和子目录 `deliverables/1b/` 是什么关系？

> **参考答案**：`1b-compatibility_check.org/.md` 是 Task 1 子任务 b（兼容性检查）的**正文报告**；`deliverables/1b/` 子目录则存放该报告的**支撑材料**（如对若干候选项目跑 Yosys 精读的 `*.sh` 脚本和 `*.log` 日志）。一个写结论，一个存证据。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**不借助工具**的纸笔任务（这是本讲的主实践，对应规格里的 practice_task）。

**任务**：在不使用 `tree`、`ls -R` 等命令的前提下，凭本讲学到的地图，徒手画出仓库的**二级目录树**，并为下列五处标注它们属于降级链的哪一段：

- `src/matmul.py`
- `scripts/pipeline`
- `rtl`
- `sim`
- `nix`

**操作步骤**：

1. 在纸上画出根节点 `LLM2FPGA/`，下面列出所有顶层目录与根级关键文件（`README.*`、`flake.nix`、`flake.lock`、`torch-mlir.nix`、`LICENSE`、`.gitmodules`）。
2. 对 `src/`、`scripts/`（含 `scripts/pipeline/`）、`rtl/`（含 `rtl/fp/`）、`fpga/`（含 `fpga/rtl/`、`fpga/constraints/`）、`sim/`、`nix/`、`patches/`（含 `patches/circt-task3-rfp/`）这些目录再展开一层，形成二级树。
3. 在那五处旁边各写一句「它属于流水线的 ___ 段」。

**预期结果（自检对照）**：

- `src/matmul.py` → **PyTorch 前端**（最小核模型本体）。
- `scripts/pipeline` → **torch-MLIR / CIRCT / Yosys 降级链**（每脚本包一站）。
- `rtl` → **降级产出的伴随手写支撑**（如浮点原语、自测外壳），服务于 SV/综合阶段。
- `sim` → **仿真与黄金参考**，服务语义等价验证（不属于降级，而是验证降级结果）。
- `nix` → **构建编排与缓存**，把上述各站段串成可缓存派生（横跨整条链）。

画完后，用 `git ls-files` 或 `ls -R` 对照检查你的树是否遗漏了顶层目录。**待本地验证**：若你画出的二级树与实际目录一致，且五处归属都答对，本讲就达标了。

## 6. 本讲小结

- 仓库目录即「流水线站段划分」：`src/`+`TinyStories/` 是前端模型，`scripts/` 是降级链，`rtl/` 是降级产物的手写支撑，`fpga/` 是上板集成，`sim/` 是验证，`nix/` 是编排缓存，`patches/` 是工具链补丁。
- 入口 `flake.nix` 在**根目录**，`nix/` 只放被它调用的模块化文件；`rtl/` 与 `fpga/rtl/` 的 `.sv` 含义不同，别混淆。
- 文档遵循「`.org` 为权威源、`.md` 自动生成」的约定，生成由 `nix run .#docs-md` 完成（实现见 `flake.nix` 的 `docs-md` app，用 `pandoc` 把 `README.org`+`deliverables/*.org`+`docs/*.org` 转成 `.md`）。
- 因此**永远不要手改 `.md`**，要改就改对应的 `.org` 再重新生成。
- `deliverables/` 的文件名遵循 `<任务号><子任务字母>-<主题>` 规则，与 `docs/project-plan_v2.md` 的 Task 1–6 一一对应；目前主要装着 Task 1–3 的产出。
- `LLM2FPGA-genAI-logs/` 是一个 git submodule（见 `.gitmodules`），是独立的日志归档仓库，不参与编译。

## 7. 下一步学习建议

到这里你已经有了一张「仓库地图」。接下来：

- 想真正把项目跑起来，请学 [u1-l3 Nix 与可复现工具链入门](u1-l3-nix-reproducible-toolchain.md)，搞懂 `flake.nix` 如何把整套版本敏感的工具钉死，并亲手执行 [u1-l4 第一个构建命令](u1-l4-first-build-and-reproduce.md)。
- 想深入某条目录，可以带着本讲的「站段归属」去读源码：先读 `src/matmul.py` + `src/matmul_adapter.py`（[u2-l1](u2-l1-models-and-adapters.md)），再顺着 `scripts/pipeline/` 一个脚本一个脚本往下走（u2/u3 系列）。
- 若你想验证「`.org` → `.md`」的细节，可直接打开 [flake.nix:608-627](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L608-L627) 的 `docsMdApp`，它不到 20 行，是理解「权威源 + 生成」最直接的样本。
