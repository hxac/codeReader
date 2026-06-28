# 使用 Tectonic 作为替代 TeX 引擎

## 1. 本讲目标

学完本讲，你应当能够：

- 说清为什么从 `latexmk` 切换到 `tectonic` 只需要改 `texlab.build.executable` 与 `texlab.build.args`，并写出 tectonic 的 **V2（`-X compile`）** 与 **V1** 两种 CLI 配置。
- 解释 `--keep-intermediates` 与 `--keep-logs` 两个 tectonic flag 分别补回了 texlab 哪两项能力（章节编号、编译告警），以及为什么它们「建议必加」。
- 把 tectonic 的 `--outdir` 与 texlab 的 `pdfDirectory`/`auxDirectory` 「两头对齐」，并理解项目里放一份 `Tectonic.toml` 会如何改变 texlab 的根目录检测。

## 2. 前置知识

本讲承接 [u2-l2「构建配置 texlab.build.*」](u2-l2-build-config.md)。请先确认你已经掌握以下几条，本讲会直接拿来用：

- **`build.executable` / `build.args` 决定「谁来编译、怎么编译」**：默认是 `latexmk` 加 `["-pdf","-interaction=nonstopmode","-synctex=1","%f"]`。换工具必须换 args，`%f` 是 `build.args` 里唯一的占位符（表示要编译的 TeX 文件）。
- **「flag 与参数必须拆成数组独立元素」**：例如要传 `-foo bar`，args 里要写 `["-foo", "bar"]` 而不是 `["-foo bar"]`。本讲会看到 `-X` 与 `compile` 正是这条规则的实例。
- **「两头对齐」原则**：只要不是用 `latexmk`（而是 tectonic 这类引擎），就必须在 args 里告诉引擎「产物输出到哪个目录」，**同时**在 `auxDirectory`/`pdfDirectory` 等设置项里告诉 texlab「去哪个目录找」。`latexmkrc` 才有自动推断，tectonic 没有这条捷径（除非用 `Tectonic.toml`，见 4.3）。
- **SyncTeX**：编译加 `-synctex=1`（latexmk）或 `--synctex`（tectonic）才会产出 `.synctex.gz` 对照表，这是 [u3-l2](u3-l2-synctex-viewers.md) 双向搜索的基础。

> 一句话定位：tectonic 与 latexmk 一样，是 texlab 通过 `build.executable` 调用的「外部编译程序」。texlab 不关心你用哪个引擎，它只负责把 `%f` 塞进 args、把命令跑起来、再去约定目录读产物。本讲全部内容，本质都是在回答「换 tectonic 后，args 怎么写、产物目录怎么对齐、哪些信息会丢」。

## 3. 本讲源码地图

本讲涉及三份 wiki 页面：

| 文件 | 作用 |
| --- | --- |
| `Tectonic.md` | 给出 tectonic 的 V2/V1 两套 `texlab.build` 配置配方，以及关于 `--keep-intermediates`/`--keep-logs`/输出目录（`--outdir`）对齐的关键提示。是本讲的主依据。 |
| `Configuration.md` | 定义 `texlab.build.*` 各项（`executable`/`args`/`auxDirectory`/`pdfDirectory`）的类型、默认值，以及 args 的拆分规则。 |
| `Project-Detection.md` | 根目录检测的四步法；其中第二步 `Tectonic.toml` 决定 tectonic 项目的 `src`/`build` 目录与 `_preamble`/`_postamble` 文件。 |

## 4. 核心概念与源码讲解

### 4.1 tectonic 引擎与 V2 / V1 两种 CLI

#### 4.1.1 概念说明

[`tectonic`](https://tectonic-typesetting.github.io/) 是一个现代化的、可替代传统 TeX 发行版的引擎。它的卖点是「自带联网下载宏包、单二进制、干净的一次性编译」。

对 texlab 而言，关键事实只有一句：**换 tectonic 不需要改 texlab 本身，只需要改 `texlab.build.*`**。因为 texlab 默认用 `latexmk` 编译，而 tectonic 是另一个外部程序，把它接进来就是改 `build.executable` 和 `build.args` 这两项——和换任何其他编译工具完全一样。

tectonic 提供两种命令行写法，wiki 把它们分别列为 **V2 CLI** 与 **V1 CLI**：

- **V2**：新写法，用 `-X` 子命令分发器，编译写作 `tectonic -X compile <文件> ...`。
- **V1**：传统写法，直接 `tectonic <文件> ...`。

两者都能完成编译，wiki 同时给出配方，具体用哪种取决于你装的 tectonic 版本与个人习惯（wiki 建议参考 `tectonic --help` 确认 flag）。

#### 4.1.2 核心流程

切换引擎的整体动作：

```
原本：build.executable = latexmk
        build.args     = ["-pdf","-interaction=nonstopmode","-synctex=1","%f"]
                          │
                          ▼  改写为 tectonic
改为：build.executable = tectonic
        build.args     =  V2: ["-X","compile","%f","--synctex","--keep-logs","--keep-intermediates"]
                          V1: ["%f","--synctex","--keep-logs","--keep-intermediates"]
                          │
                          ▼
texlab 仍只做：把 %f 替换成当前 TeX 文件路径 → 执行 tectonic → 去约定目录读产物
```

注意三点：

1. `%f` 还是那个 `%f`（要编译的 TeX 文件路径），与 latexmk 配置里完全相同。
2. V2 比 V1 在最前面多了 `"-X"` 和 `"compile"` 两个元素——这是 `-X compile` 子命令，按拆分规则拆成两项。
3. latexmk 用 `-synctex=1`，tectonic 用 `--synctex`（无 `=1`）。**两者都必不可少**：少了它就不产 `.synctex.gz`，[u3-l2](u3-l2-synctex-viewers.md) 的双向搜索会直接失效。

#### 4.1.3 源码精读

先看 tectonic 的总体说明。[Tectonic.md:L1-L4](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Tectonic.md#L1-L4) 点明三件事：tectonic 是「modernized, alternative TeX engine」；texlab 的大多数功能在 tectonic 下「work out of the box」；但「to compile documents through texlab, you need to change the configuration」，并提示看 `tectonic --help` 了解 flag。

再看 V2 配方。[Tectonic.md:L18-L34](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Tectonic.md#L18-L34) 给出的完整 JSON：

```json
{
  "texlab.build.executable": "tectonic",
  "texlab.build.args": ["-X", "compile", "%f", "--synctex", "--keep-logs", "--keep-intermediates"],
  "texlab.build.pdfDirectory": "build",
  "texlab.build.auxDirectory": "build"
}
```

以及 V1 配方。[Tectonic.md:L36-L50](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Tectonic.md#L36-L50)：

```json
{
  "texlab.build.executable": "tectonic",
  "texlab.build.args": ["%f", "--synctex", "--keep-logs", "--keep-intermediates"],
  "texlab.build.pdfDirectory": "build",
  "texlab.build.auxDirectory": "build"
}
```

两份配方唯一区别就是 args 最前面有没有 `"-X","compile"`。

这两份配方里有两个细节值得对照 [Configuration.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md)：

- **`-X` 与 `compile` 必须分开写**。这正是 [Configuration.md:L17-L22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L17-L22) 那条拆分规则的活例：`-X` 是 flag，`compile` 是它的参数（子命令名），所以要写成两个数组元素，不能写成 `"-X compile"`。
- **`%f` 仍是唯一占位符**。[Configuration.md:L24-L26](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L24-L26) 说明 `%f` 是「The path of the TeX file to compile」，在 `build.args` 里由 texlab 替换。换 tectonic 不改变这点。

> 一个常被忽略的点：`build.executable` 默认是 `latexmk`（见 [Configuration.md:L5-L12](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L5-L12)）。所以「零配置可用」的前提是你装了 latexmk。如果你只装了 tectonic、没装 latexmk，连默认编译都会失败——这正是你必须显式写上面这份配置的根本原因。

#### 4.1.4 代码实践

1. **实践目标**：验证「改 executable + args 即可切换引擎」这件事，先把 `--keep-*` 和目录对齐放一边，只看 tectonic 能否被 texlab 调起来编译。
2. **操作步骤**：
   - 确认本机已安装 `tectonic`（`tectonic --version` 可用）。
   - 写一个最小 `main.tex`（含 `\documentclass{article}` 与 `document` 环境）。
   - 用最小 tectonic 配置（**示例配置**，先不加 `--keep-*`，故意为之）：
     ```json
     {
       "texlab.build.executable": "tectonic",
       "texlab.build.args": ["-X", "compile", "%f"]
     }
     ```
   - 触发一次编译（保存文件并开 `texlab.build.onSave`，或由编辑器发起 build）。
3. **需要观察的现象**：texlab 调用了 `tectonic -X compile main.tex`，并在与 `main.tex` 同级目录产出 `main.pdf`。
4. **预期结果**：PDF 正常生成。**但同时你会发现**：补全里看不到章节编号、诊断里看不到编译告警——这正是下一节要解决的问题（因为这里故意省了 `--keep-intermediates`/`--keep-logs`）。
5. 说明：本步只验证「引擎切换」打通；完整可用配置见 4.2、4.3。

#### 4.1.5 小练习与答案

**练习 1**：V2 与 V1 两份配方的 `build.args` 差在哪里？为什么那个差异要拆成两个数组元素？

**答案**：V2 在最前面多了 `"-X"` 和 `"compile"`（即 `-X compile` 子命令），V1 没有这两项。因为 `-X` 是 flag、`compile` 是它的参数，按 [Configuration.md:L17-L22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L17-L22) 的拆分规则，flag 与参数必须分开成两个独立元素，不能写成 `"-X compile"`。

**练习 2**：把 latexmk 配置里的 `-synctex=1` 直接照搬到 tectonic 的 args 里行不行？

**答案**：不行。latexmk 用 `-synctex=1`，tectonic 用 `--synctex`（见 [Tectonic.md:L18-L34](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Tectonic.md#L18-L34)）。flag 名字是各引擎自己的约定，照抄会报错或不生效；少了它就不产 `.synctex.gz`，[u3-l2](u3-l2-synctex-viewers.md) 的双向搜索会失效。

---

### 4.2 keep-intermediates 与 keep-logs：把信息还给 texlab

#### 4.2.1 概念说明

tectonic 的设计哲学是「干净的一次性编译」——编译成功后，它默认会**清理掉中间文件和日志**，只留下最终的 PDF。这对单纯排版很舒服，但对 texlab 是个麻烦：

- texlab 需要读 `.aux` 等**中间文件**，才能算出 `\section`、`\subsection` 的**章节编号**，并把编号显示在补全项里（比如让你看到「3.2 Methods」而不仅是「Methods」）。
- texlab 需要读 `.log` **日志文件**，才能把引擎吐出的 **编译告警**（如未定义引用、字体替换）作为诊断上报给编辑器。

所以 wiki 给出两个「建议必加」的 flag：

- `--keep-intermediates`：保留中间文件 → texlab 能拿到章节编号 → 补全里显示编号。
- `--keep-logs`：保留日志 → texlab 能上报编译告警。

这两个 flag 的本质，是**扭转 tectonic「编译完即清理」的默认行为**，把 texlab 依赖的那两类产物留下来。

#### 4.2.2 核心流程

```
tectonic 编译完成
        │
        ├─ 默认行为：清理中间文件 + 日志，只留 PDF
        │      → texlab 拿不到 .aux / .log
        │      → 补全无章节编号、诊断无编译告警 ❌
        │
        └─ 加 --keep-intermediates：保留 .aux 等
              → texlab 读到交叉引用计数 → 算出章节编号 → 补全显示编号 ✅
           加 --keep-logs：保留 .log
              → texlab 解析告警 → 作为诊断上报 ✅
```

一句话：`--keep-intermediates` 管编号，`--keep-logs` 管告警。

#### 4.2.3 源码精读

这条建议写在 `Tectonic.md` 顶部的 Hint 块里。[Tectonic.md:L12-L14](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Tectonic.md#L12-L14) 原文：

- `--keep-intermediates` is recommended because they allow `texlab` to find out the **section numbers** and show them in the **completion**.
- Without the `--keep-logs` flag, `texlab` won't be able to report compilation **warnings**.

把这两行对照 4.1.3 的两份配方看：无论 V2 还是 V1，args 里都同时带了 `--keep-logs` 与 `--keep-intermediates`——这就是 Hint 落到配置上的结果。换句话说，wiki 给出的配方不是「最小可编译配置」，而是「既能让编译跑通、又能让 texlab 各项功能正常」的**推荐配置**。

> 与 latexmk 的对比：latexmk 默认就会留下 `.aux`、`.log` 等一堆中间文件（甚至被人嫌「太脏」），所以从 latexmk 切到 tectonic 时，**最容易踩的坑就是忘了加这两个 flag**，结果「PDF 能出，但补全编号和告警都消失了」。这正是 wiki 把它放进顶部 Hint 的原因。

#### 4.2.4 代码实践

1. **实践目标**：亲手对比「加 flag」与「不加 flag」时 texlab 行为的差异。
2. **操作步骤**：
   - 沿用 4.1.4 的 `main.tex`，里面写两个带 `\label` 的 `\section`，并在某处写一个**未定义的 `\ref{notexist}`**（用来制造编译告警）。
   - 第一次：用 4.1.4 的最小配置（不带 `--keep-*`）编译，记录补全里 `\ref{` 后是否显示带编号的章节、诊断面板是否有告警。
   - 第二次：把 args 改成完整配方 `["-X","compile","%f","--synctex","--keep-logs","--keep-intermediates"]` 重新编译。
3. **需要观察的现象**：
   - 第一次：补全的章节项**没有编号**、诊断里**看不到**关于 `\ref{notexist}` 的告警。
   - 第二次：补全的章节项**带上编号**（如 `1.1`）、诊断里**出现**「未定义引用」之类的告警。
4. **预期结果**：加上两个 flag 后，章节编号与编译告警双双恢复。
5. 说明：若第二次仍未显示编号/告警，先排查编译是否真的成功、产物目录是否被找对（见 4.3）。具体能否在你环境中复现，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么从 latexmk 迁到 tectonic 后，章节编号突然从补全里消失了？

**答案**：latexmk 默认保留 `.aux` 等中间文件，texlab 能读到、算出编号；tectonic 默认编译完就清理中间文件，texlab 读不到 `.aux`，自然算不出编号。加上 `--keep-intermediates` 保留中间文件即可恢复（见 [Tectonic.md:L12-L13](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Tectonic.md#L12-L13)）。

**练习 2**：加了 `--keep-intermediates` 但**没加** `--keep-logs`，会出现什么现象？

**答案**：补全里章节编号正常（中间文件保留了），但诊断面板里看不到编译告警（日志被清理了，texlab 无从解析）。要让告警也回来，必须再加 `--keep-logs`（见 [Tectonic.md:L14](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Tectonic.md#L14)）。两个 flag 各管一摊，不能互相替代。

---

### 4.3 输出目录对齐与 Tectonic.toml 的项目识别

#### 4.3.1 概念说明

这是 u2-l2「两头对齐」原则在 tectonic 上的落地。tectonic 不是 latexmk，所以**没有 `latexmkrc` 那条自动推断产物目录的捷径**——你必须自己保证「引擎往哪输出」和「texlab 去哪找」指向同一个目录。

具体来说有两个方向要对齐：

- **args 侧**：用 tectonic 的 `--outdir <目录>` 告诉引擎把 PDF/中间文件输出到哪。
- **设置项侧**：用 `texlab.build.pdfDirectory` 和 `texlab.build.auxDirectory` 告诉 texlab 去哪个目录读 PDF 和 `.aux`。

两边必须一致，否则会出现「编译成功了，但 texlab 找不到 PDF/编号/告警」的诡异现象。

此外，tectonic 项目常常会放一份 **`Tectonic.toml`** 清单文件（声明源文件、输出目录等）。对 texlab 而言，`Tectonic.toml` 不只是 tectonic 自己的配置——它还是**根目录检测的一个信号**：一旦项目里出现 `Tectonic.toml`，texlab 会改用它约定的 `src`/`build` 目录结构。

#### 4.3.2 核心流程

两条互相关联的路径：

```
路径 A：手动对齐（无 Tectonic.toml）
   args:        ... --outdir build          ← 引擎输出到 build/
   设置项:       pdfDirectory = "build"      ← texlab 去 build/ 找 PDF
                auxDirectory = "build"      ← texlab 去 build/ 找 .aux
   两边都指向 build/ ✅

路径 B：放一份 Tectonic.toml
   texlab 根目录检测第二步命中 Tectonic.toml
        → 自动采用清单里的 src/ 与 build/ 目录
        → 并把 _preamble.tex / _postamble.tex（若存在）并入项目
```

> 联系 [u1-l2](u1-l2-project-detection.md)：根目录检测有四步优先级——`.texlabroot` → `Tectonic.toml` → `.latexmkrc` → 根源文件。latexmk 用户靠第三步（`.latexmkrc`）获得目录自动推断；tectonic 用户对应的「自动推断」入口是第二步（`Tectonic.toml`）。两条路各对应一类引擎。

#### 4.3.3 源码精读

**对齐提示**。[Tectonic.md:L10](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Tectonic.md#L10) 明确写：要把 `texlab.build.pdfDirectory` 和 `texlab.build.auxDirectory` 设成「the configured output directory (see `--outdir` argument)」。也就是说，你用 tectonic 的 `--outdir` 把输出指向哪里，这两个设置项就得跟着指到哪里。

wiki 给出的两份配方（4.1.3）正是这么做的：它们都把 `pdfDirectory` 与 `auxDirectory` 设为 `"build"`。配方的 args 聚焦展示 texlab 关心的几个 flag（`--synctex`/`--keep-logs`/`--keep-intermediates`），而 `--outdir` 是 tectonic 自身的普通输出 flag（wiki 在 [Tectonic.md:L4](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Tectonic.md#L4) 提示去看 `tectonic --help`）。要把配方补成「真正输出到 build/」的完整配置，需在 args 里补上 `--outdir build`，与设置项对齐——**示例（补齐对齐侧）**：

```json
{
  "texlab.build.executable": "tectonic",
  "texlab.build.args": ["-X", "compile", "%f", "--synctex",
                        "--keep-logs", "--keep-intermediates", "--outdir", "build"],
  "texlab.build.pdfDirectory": "build",
  "texlab.build.auxDirectory": "build"
}
```

注意 `--outdir` 与 `build` 仍是两个独立数组元素（拆分规则）。如果只改设置项、不在 args 里加 `--outdir`，tectonic 会把产物吐到当前目录，而 texlab 却去 `build/` 找，于是「找不到」。

这两个设置项本身的语义见 Configuration.md：

- [Configuration.md:L66-L76](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L66-L76)：`texlab.build.auxDirectory`——「When not using latexmk」时定义 `.aux` 所在目录，且**必须在 args 里也设置**（"you need to set the aux directory in `latex.build.args` too"）；用 `latexmkrc` 时 texlab 才自动推断。
- [Configuration.md:L92-L102](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L92-L102)：`texlab.build.pdfDirectory`——同理，「you need to set the output directory in `latex.build.args` too」。

这两段措辞就是「两头对齐」原则的官方表述：**设置项和 args 必须同时改、指向同一处**。

**Tectonic.toml 的项目识别**。根目录检测的四步法见 [Project-Detection.md:L17-L24](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L17-L24)，其中第二步专门处理 tectonic：

> 「Check if there is a `Tectonic.toml` manifest. Then, the server uses the `src` and `build` directories and adds `_preamble.tex` and `_postamble.tex` to the project (if present).」

见 [Project-Detection.md:L21](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L21)。它的含义是：一旦命中 `Tectonic.toml`，texlab 不再需要你手动对齐目录，而是直接采用清单里声明的 `src`（源文件目录）与 `build`（输出目录），并把 tectonic 约定的 `_preamble.tex`、`_postamble.tex`（如果存在）自动并入项目。这正是 tectonic 项目「免手动对齐」的入口。

> 一个对照：latexmkrc（第三步）让 texlab 自动推断产物目录；`Tectonic.toml`（第二步）让 texlab 自动采用 src/build。两者都是「靠一份引擎侧的清单文件换取目录自动识别」，但分属不同引擎、不同优先级。两者都没有时，才退回第四步（用根源文件所在目录），此时你就必须像路径 A 那样手动两头对齐。

#### 4.3.4 代码实践

1. **实践目标**：体会「两头不对齐会出什么问题」，并掌握两种对齐方式（手动 `--outdir` 与 `Tectonic.toml`）。
2. **操作步骤**：
   - **先制造故障**：把 `pdfDirectory`/`auxDirectory` 设为 `"build"`，但 args 里**故意不加** `--outdir build`，编译一次。
   - **再修复（路径 A）**：在 args 末尾补上 `"--outdir","build"`，重新编译。
   - **换路径 B**：在项目根放一份最小 `Tectonic.toml`（声明 `src` 与 `build` 目录，**示例**，具体字段以 tectonic 文档为准），把手动 `--outdir` 与目录设置项移除，重新编译。
3. **需要观察的现象**：
   - 故障步：PDF 实际输出在源文件同级目录，但 texlab 去 `build/` 找，于是正向搜索打不开 PDF、补全可能缺编号。
   - 路径 A：产物进入 `build/`，texlab 在 `build/` 找到 PDF 与 `.aux`，一切正常。
   - 路径 B：texlab 命中 `Tectonic.toml` 后自动采用清单的 `build` 目录，无需手动对齐。
4. **预期结果**：路径 A 与路径 B 下，编译产物、章节编号、编译告警都能被 texlab 正确识别。
5. 说明：`Tectonic.toml` 的具体字段写法以 tectonic 官方文档为准，本讲只确认它对 **texlab 根目录检测** 的影响；能否在你环境中跑通，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：你在 args 里加了 `--outdir build`，却忘了改 `pdfDirectory`/`auxDirectory`（仍是默认 `.`）。会出现什么？

**答案**：tectonic 把 PDF 与中间文件输出到 `build/`，但 texlab 仍到根目录（`.`）去找，于是找不到 PDF（正向搜索打不开）、也读不到 `build/` 里的 `.aux`（章节编号消失）。必须把这两个设置项也改成 `"build"`，与 `--outdir` 对齐（见 [Tectonic.md:L10](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Tectonic.md#L10) 与 [Configuration.md:L92-L102](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L92-L102)）。

**练习 2**：项目里有 `Tectonic.toml`、也有 `.latexmkrc`，texlab 会用哪个的目录约定？

**答案**：用 `Tectonic.toml` 的。根目录检测有严格优先级（见 [Project-Detection.md:L17-L24](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L17-L24)）：`.texlabroot` → `Tectonic.toml` → `.latexmkrc` → 根源文件。`Tectonic.toml` 排在 `.latexmkrc` 前面，命中即停，所以后者不会再被考虑。

## 5. 综合实践

把本讲三个模块串起来，完成一次「latexmk → tectonic」的迁移：

1. **起点**：准备一个用默认 latexmk 能正常编译的多节工程（`main.tex` 含两个带 `\label` 的 `\section`，并故意写一个 `\ref{notexist}` 制造告警）。
2. **改写为 tectonic**（任选 V1 或 V2）：
   - 把 `build.executable` 改成 `"tectonic"`，args 用 4.1.3 的配方（务必带 `--synctex`、`--keep-logs`、`--keep-intermediates`）。
   - 选一种对齐方式：
     - **路径 A**：args 加 `"--outdir","build"`，并把 `pdfDirectory`/`auxDirectory` 都设为 `"build"`。
     - **路径 B**：在根目录放一份声明了 `src`/`build` 的 `Tectonic.toml`，不写手动目录项。
3. **编译并逐项验收**（对应三个模块）：
   - **引擎切换（4.1）**：保存后 texlab 调起 tectonic，`build/` 下出现 `main.pdf`（以及 `.synctex.gz`）。
   - **信息保留（4.2）**：补全里章节项**带编号**；诊断面板**出现**关于 `\ref{notexist}` 的告警。
   - **目录对齐（4.3）**：从编辑器发起正向搜索（需配好 [u3-l2](u3-l2-synctex-viewers.md) 的 `forwardSearch.*`），PDF 能被正确打开并跳转——证明 texlab 在 `build/` 找到了 PDF。
4. **记录**：用一张表标注「每个能力由哪个 flag / 设置项支撑」（编号←`--keep-intermediates`、告警←`--keep-logs`、SyncTeX←`--synctex`、产物目录←`--outdir`↔`pdfDirectory`/`auxDirectory` 或 `Tectonic.toml`）。

> 若章节编号或告警缺失，先查 `--keep-*` 是否都在；若 PDF 打不开，先查 `--outdir` 与 `pdfDirectory`/`auxDirectory` 是否指向同一目录。

## 6. 本讲小结

- 换 tectonic 只需改 `texlab.build.executable` 与 `texlab.build.args`；V2 用 `-X compile` 子命令（拆成 `"-X","compile"` 两元素），V1 直接 `"%f",...`，两者都靠 `%f` 这个唯一占位符传文件路径。
- `--keep-intermediates` 保留 `.aux` 等中间文件，让 texlab 算出并显示**章节编号**；`--keep-logs` 保留日志，让 texlab 上报**编译告警**——tectonic 默认会清理这两类产物，故建议必加。
- tectonic 没有 latexmkrc 的自动推断，产物目录必须「两头对齐」：args 里用 `--outdir <目录>`，设置项里把 `pdfDirectory`/`auxDirectory` 指到同一目录。
- 项目里若有 `Tectonic.toml`，texlab 根目录检测会命中第二步，自动采用其 `src`/`build` 目录并把 `_preamble.tex`/`_postamble.tex` 并入项目——这是 tectonic 的「免手动对齐」入口。
- `--synctex` 产出 `.synctex.gz`，是 [u3-l2](u3-l2-synctex-viewers.md) 双向搜索的前提；latexmk 的 `-synctex=1` 与 tectonic 的 `--synctex` 是各自引擎的写法，不能混用。

## 7. 下一步学习建议

- 想了解编译在协议层是如何被 `textDocument/build` 自定义请求触发的，进入 [u4-l1「自定义 LSP 消息：build 与 forwardSearch」](u4-l1-custom-lsp-messages.md)。
- 想可视化 tectonic 项目被 texlab 识别后的依赖结构，回到 [u1-l2](u1-l2-project-detection.md) 复习依赖树，并参见 [u4-l2](u4-l2-workspace-commands.md) 的 `texlab.showDependencyGraph` 命令。
- 如果你从 [u3-l2](u3-l2-synctex-viewers.md) 跳来确认 tectonic 是否支持双向搜索——答案是需要显式加 `--synctex`，配齐后即可沿用 u3-l2 的全部阅读器配方。
