# u5-l4 分阶段 Yosys 综合与 targeted memory_map

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清楚为什么要把 Yosys 的 `synth_xilinx` 这条「一体化综合脚本」拆成 9 个可观察的阶段，以及这种拆分带来的两个好处：**可观察性**与**可在中间插入自定义步骤**。
- 看懂 `flake.nix` 中 `mkSynthJsonStages` 如何用 `mkSynthStageIl` / `mkSynthStageMemoryMapIl` / `mkSynthStageJson` 三个模具把 `synth_xilinx -run <from>:<to>` 包成 9 个首尾相接、各自可缓存的 Nix 派生。
- 理解 `stage4` 的 **targeted memory_map**：为什么先用 `awk` 在 RTLIL 里挑出含 `cell $mem` 的模块，再只对这些模块逐个执行 `memory_map`，而不是让 `synth_xilinx` 一刀切地处理全部内存。
- 掌握 `stage9` 前的 `filter_rtlil_modules.py` 如何按「`\` + 大写字母」命名规则剥离「待确认模块」，让最终的 `write_json` 不把这些模块的内部展开成叶单元。

本讲承接 u5-l3（资源利用报告）的「上游」：u5-l3 讲的是 `write_utilization_report.py` 如何**消费**一份 mapped JSON；本讲讲的是这份 mapped JSON 是**如何经过 9 个阶段**生产出来的。

## 2. 前置知识

在进入本讲前，读者需要具备以下概念（前几讲已建立）：

- **RTLIL**：Yosys 内部的中间表示，对应磁盘上的 `.il` 文本文件。本讲中每个阶段的输入和输出都是一份 RTLIL。（见 u5-l1）
- **`synth_xilinx`**：Yosys 自带的 Xilinx 7 系综合脚本，本身是一条由许多 pass 组成的长流水线，内部按「标签（label）」分段。一条 `synth_xilinx -family xc7 -top main -noiopad -json out.json` 会从头跑到尾。
- **派生（derivation）与 `runCommand`**：Nix 把每一段命令包成一个派生，输入 + 命令 + 环境的指纹决定它的存储路径，因此可被缓存复用。（见 u3-l5、u5-l2）
- **`$mem` 与 `memory_map`**：Yosys 用抽象的 `$mem` / `$memrd` / `$memwr` 单元表示内存；`memory_map` 这条 pass 会把它们展开成地址译码逻辑 + 存储单元（触发器/寄存器），是一种**与具体工艺无关、偏悲观**的展开，区别于 `memory_bram`（把内存打进 BRAM 块）。
- **escaped 名字**：RTLIL 里以反斜杠 `\` 开头的标识符是「原样保留」的名字（来自上游设计而非 Yosys 自动生成），例如 `\handshake_memory0`。以 `$` 开头的则是 Yosys 自动生成的内部名。

一个关键直觉：**`synth_xilinx` 是一个「黑盒长脚本」，但 Yosys 给了 `-run <from>:<to>` 这个旋钮，允许我们只执行其中一段。** 本讲的核心，就是把这条长脚本切成 9 段，并在中间替换掉它的内存处理步骤。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `flake.nix` | 定义 `mkSynthStageIl`、`mkSynthStageMemoryMapIl`、`mkSynthStageJson`、`mkSynthJsonStages`，以及把它们串起来的 `mkTinyStoriesSelftestBundle`。是本讲的主角。 |
| `scripts/pipeline/filter_rtlil_modules.py` | 在 `stage9` 前按模块名正则过滤 RTLIL，剥离 `\` + 大写字母开头的「待确认模块」定义。 |
| `deliverables/3e-tiny-stories-1m-resource-report.md` | 用一句话点明这条「staged Yosys/Xilinx mapping」流水线在整个 TinyStories-1M 资源评估里的位置，是动机的权威来源。 |

注意：本讲描述的「分阶段综合」**只服务于 TinyStories（含自测外壳）的大设计流程**（`mkTinyStoriesSelftestBundle` → `mkSynthJsonStages`）。小设计 matmul 用的是 u5-l2 讲过的一体化 `mkSynthJson`（一条 `synth_xilinx ... -json $out` 跑完），不需要分阶段。

## 4. 核心概念与源码讲解

### 4.1 synth_xilinx 分阶段 `-run`：把一体化综合拆成可观察阶段

#### 4.1.1 概念说明

`synth_xilinx` 是 Yosys 自带的、面向 Xilinx 7 系 FPGA 的综合脚本。它内部不是一团乱麻，而是由若干**带标签的阶段（labeled blocks）**顺序组成，典型标签有 `begin`、`prepare`、`coarse`、`map_memory`、`fine`、`map_cells`、`map_ffs`、`map_luts`、`check`。每个标签背后挂着若干条 pass。

Yosys 允许用 `-run <from>:<to>` 只执行其中一段：`<from>` 是开始执行的标签，`<to>` 是停止的标签。本讲会看到两种用法：

- **两端同名 `X:X`**（如 `fine:fine`、`map_cells:map_cells`）：单独执行某一个阶段。
- **两端异名 `A:B`**（如 `coarse:map_memory`、`map_luts:check`）：连续执行多个阶段。

为什么要拆？对于 TinyStories-1M 这种「比目标芯片大约 141 倍」的超大设计，有两个工程诉求：

1. **可观察性**：把综合切成 9 段，每段写出一份 RTLIL，就能在任意中间点 `stat` 看资源曲线，定位到底是哪一步让 LUT 爆炸——而不是只看到一个最终 JSON。
2. **可插入自定义步骤**：`synth_xilinx` 自带的内存处理是「一刀切」的。本项目想用一套**定向的** `memory_map`（见 4.2）来替换它，这就要求先把综合「断开」，在断点处插自己的 pass，再继续。

#### 4.1.2 核心流程

`mkSynthStageIl` 是 8 个 RTLIL 阶段（stage1–stage8）共用的模具。每个阶段的 `run.ys` 都遵循同一个骨架：

```text
read_rtlil <上一阶段产物>      # 读入上一阶段的 RTLIL
[read_slang <顶层 SV>]          # 仅 stage1 读自测外壳 SV
hierarchy -top <topName> -check # 锁定顶层并校验层次
<preCommands>                   # 仅 stage1 多一条 proc
<commands>                      # 本阶段真正要跑的 synth_xilinx -run ... 或 opt
write_rtlil $out                # 写出本阶段 RTLIL，交给下一阶段
```

关键设计：**阶段之间唯一的耦合就是「上一阶段的 `$out` = 下一阶段的 `read_rtlil` 输入」**。每段都是一次独立的 Yosys 调用，因此各自是一个独立的 Nix 派生，可单独缓存。改了 stage3 的命令，只会让 stage3 及其下游（stage4…stage9）重算，stage1、stage2 命中缓存。

#### 4.1.3 源码精读

先看通用模具 `mkSynthStageIl`：

[flake.nix:280-307](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L280-L307) — 把一段 Yosys 命令包成 `runCommand`，注意 `run.ys` 的拼接顺序：`read_rtlil` → 可选 `read_slang` → `hierarchy -top -check` → `preCommands` → `commands` → `write_rtlil $out`。Nix 自动注入的 `$out` 既是产物路径也被写进 `run.ys` 末尾。

再看 9 个阶段的编排 `mkSynthJsonStages`：

[flake.nix:386-472](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L386-L472) — 用 `rec` 让后一个 stage 引用前一个 stage 的产物（如 `inputIl = stage3;`），把 9 段首尾串成链；最末 `json = stage9;` 把 JSON 暴露给消费方。

各阶段一一对照如下：

| 阶段 | 源码 | 关键命令 | 作用 |
| --- | --- | --- | --- |
| stage1 | [flake.nix:388-397](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L388-L397) | `proc` + `synth_xilinx -run begin:prepare` | 唯一一个带 `topSv` 的阶段：把模型 RTLIL 与自测外壳 SV 合并，确立顶层，做综合前预处理 |
| stage2 | [flake.nix:399-407](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L399-L407) | `synth_xilinx -run coarse:map_memory` | 粗粒度优化，运行到内存映射段 |
| stage3 | [flake.nix:409-415](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L409-L415) | `opt -fast -full` | 额外插一段全量优化（不在 `synth_xilinx` 标准段内，是本项目自定义插入） |
| stage4 | [flake.nix:417-422](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L417-L422) | 走专用模具 `mkSynthStageMemoryMapIl` | **targeted memory_map**（见 4.2） |
| stage5 | [flake.nix:424-432](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L424-L432) | `synth_xilinx -run fine:fine` | 细粒度优化 |
| stage6 | [flake.nix:434-442](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L434-L442) | `synth_xilinx -run map_cells:map_cells` | 把逻辑映射到 Xilinx 元胞（DSP、进位链等） |
| stage7 | [flake.nix:444-452](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L444-L452) | `synth_xilinx -run map_ffs:map_ffs` | 映射触发器到 Xilinx FF 类型 |
| stage8 | [flake.nix:454-462](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L454-L462) | `synth_xilinx -run map_luts:check` | LUT 映射（`abc`）+ 最终完整性检查 |
| stage9 | [flake.nix:464-469](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L464-L469) | 走专用模具 `mkSynthStageJson` | 剥离待确认模块后 `proc` + `write_json`（见 4.3） |

注意三个细节：

1. **只有 stage1 带 `topSv`**：因为只有 stage1 需要把模型 RTLIL 与 `tiny_stories_selftest_top.sv` 合并；之后阶段都只在已合并的 RTLIL 上推进。
2. **stage1 的 `preCommands = [ "proc" ]`**：`proc` 把 RTLIL 里的过程（always 块）翻译成数据流图，是后续 `synth_xilinx` 的前置条件。
3. **stage3 是「自定义插入」**：`opt -fast -full` 不在任何 `synth_xilinx -run` 区间里，它就是一条独立的优化 pass，被当作一个独立阶段插了进来——这正是「拆阶段」带来的灵活性。

最后看一眼这条链的调用方：

[flake.nix:566-606](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L566-L606) — `mkTinyStoriesSelftestBundle` 把「模型 opt → 外部化大内存 → 自测外壳 + 分阶段综合 → 资源报告」串成 bundle；`stages = mkSynthJsonStages { ... }` 是其中一环，最终 `designJson = stages.json` 喂给 `mkMappedJsonUtilizationReport`（即 u5-l3 的报告脚本）。

#### 4.1.4 代码实践

**实践目标**：直观感受「分阶段」带来的可观察性——拿到 9 份中间 RTLIL，看资源随阶段变化。

**操作步骤**（源码阅读 + 本地运行结合）：

1. 先确认 9 个阶段的产物确实是独立的 `.il` 文件：阅读 [flake.nix:386-472](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L386-L472)，注意每个 `mkSynthStageIl` 的派生名是 `${name}-${stageId}.il`（如 `tiny-stories-1m-...-stage3.il`）。
2. （可选，本地执行）跑一遍 gate 命令：
   ```bash
   nix build .#tiny-stories-1m-baseline-float-selftest-all-memory-utilization -L
   ```
   然后在构建日志里观察 `[mkSynthJson:...] stageN ...` 这类 stderr 行——它们来自模具里的 `echo "[mkSynthJson:${name}] ${stageLabel}" >&2`。
3. （源码阅读）回答：为什么 stage2 的 `-run` 写成 `coarse:map_memory`，而 stage5/6/7 都写成 `X:X`？提示：前者要跨多个标签，后者只想隔离单个标签。

**需要观察的现象**：构建日志里 9 条 `[mkSynthJson] stageN` 行按顺序出现；每条对应一次独立的 Yosys 调用。

**预期结果**：能复述「stage2 跨段、stage5/6/7 单段」的原因。

**待本地验证**：完整 TinyStories 构建耗时长、内存大，若机器无法跑通，至少完成第 1、3 步的源码阅读部分。

#### 4.1.5 小练习与答案

**练习 1**：如果只想观察「LUT 映射前后」的资源差，应该取哪两份中间 RTLIL 做对比？

> **答案**：stage7（`map_ffs` 之后、`map_luts` 之前）与 stage8（`map_luts:check` 之后）的两份 `.il`。前者是 LUT 映射前的状态，后者是映射后。

**练习 2**：把 stage3 的 `opt -fast -full` 删掉，哪些派生会失效、哪些不受影响？

> **答案**：stage3 自身及其全部下游（stage4…stage9）的指纹会变、需重算；stage1、stage2 因输入和命令未变而命中缓存。

---

### 4.2 targeted memory_map：只对含 `$mem` 的模块做展开

#### 4.2.1 概念说明

`synth_xilinx` 自带的内存处理（`map_memory` 段）会对整个设计里的内存「一刀切」。但本项目在进入综合之前，已经用 `externalize_large_memories.py`（见 u6-l2）把超大 Handshake 存储外部化成了 blackbox 候选。剩下的 `$mem` 单元是中小型的，项目希望对它们做**定向的、可控的** `memory_map`，而不是让 `synth_xilinx` 重新接管全部内存决策。

> 小贴士：`memory_map` 把 `$mem` 展开成地址译码 + 触发器/寄存器，是「工艺无关、偏悲观」的展开——它会让存储位变成大量 FF/LUT。这与 `memory_bram`（把内存打进 BRAM 块）是两种不同策略。本项目的资源评估有意采用展开式估算，得到的是偏上界的结果。

#### 4.2.2 核心流程

stage4 用专用模具 `mkSynthStageMemoryMapIl`，分三步：

```text
1. awk 扫描输入 RTLIL：
   - 遇到 ^module 行，记下当前模块名（$2）
   - 遇到缩进的 'cell $mem' 行，把当前模块标记为「含内存」
   - END 打印所有「含内存」的模块名，排序后写入 stage-modules.txt

2. 拼装 run.ys：
   read_rtlil <inputIl>
   hierarchy -top <topName> -check
   （对 stage-modules.txt 里每个模块，追加三条命令）
     cd <模块名>      # 进入该模块作用域
     memory_map       # 只对它展开内存
     cd ..            # 回到根
   write_rtlil $out

3. 跑 yosys -s run.ys，写出 stage4.il
```

为什么必须 `cd <模块>` / `cd ..`？因为 `memory_map` 默认作用于「当前模块」，而 Yosys 的 `cd` 就是切换当前工作模块。这样就能精确地「只展开含 `$mem` 的模块、不动其它模块」。

#### 4.2.3 源码精读

[flake.nix:309-351](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L309-L351) — stage4 的全部逻辑。重点看三段：

- 第 312–319 行的 `awk`：`/^module / { mod = $2 }` 跟踪当前模块；`/^[[:space:]]*cell \$mem/ { mods[mod] = 1 }` 记下含内存的模块；`END` 时统一输出并 `| sort`。注意正则里的 `\$mem` 是为了在 awk 里转义 `$`，匹配的就是 RTLIL 里的 `cell $mem` 行。
- 第 321–324 行：先写好固定的 `read_rtlil` + `hierarchy` 头。
- 第 326–332 行的 `while read`：逐行读 `stage-modules.txt`，对每个模块名追加 `cd` / `memory_map` / `cd ..` 三条命令。这里**先 `sort` 再 `while read`**，保证模块处理顺序确定（与字母序一致），让派生可复现。

再看它在阶段链里的位置：

[flake.nix:417-422](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L417-L422) — stage4 的 `inputIl = stage3`，即承接 stage3 的全量优化结果；它不用 `mkSynthStageIl` 而用 `mkSynthStageMemoryMapIl`，这正是「在 stage3 与 stage5 之间替换掉内存处理」的断点。

#### 4.2.4 代码实践

**实践目标**：理解 `awk` 与 `while read` 的配合，能预测一个迷你 RTLIL 输入会得到哪些模块被 `memory_map`。

**操作步骤**：

1. 阅读第 312–319 行的 awk 程序（[同上链接](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L312-L319)）。
2. 构造一份示例 RTLIL（**示例代码，非项目产物**）：
   ```text
   module \Foo
     memory \mem
     cell $mem ...
   end
   module \Bar
     cell $dff ...
   end
   ```
3. 在脑子里跑这段 awk：`^module ` 命中两行，`mod` 分别变为 `\Foo`、`\Bar`；只有 `\Foo` 内部出现 `cell $mem`，所以 `mods` 里只有 `\Foo`。

**需要观察的现象**：`stage-modules.txt` 只应包含 `\Foo`，不包含 `\Bar`。

**预期结果**：run.ys 里只会出现 `cd \Foo` / `memory_map` / `cd ..`，`\Bar` 不被处理。

**待本地验证**：可把示例写进文件，用 `gawk` 实际跑一遍验证预测。

#### 4.2.5 小练习与答案

**练习 1**：为什么 awk 里匹配的是 `cell $mem` 而不是 `memory`？两者在 RTLIL 里分别代表什么？

> **答案**：`memory` 是 RTLIL 里内存对象的声明行（`memory \mem`），而 `cell $mem` 才是真正实例化内存单元的行。`memory_map` 作用的对象是 `$mem` 单元，因此以 `cell $mem` 作为「该模块含待展开内存」的判据更准确。

**练习 2**：如果跳过 `sort` 直接 `while read`，会对正确性有影响吗？对可复现性呢？

> **答案**：不影响正确性（每个含内存的模块都会被 `memory_map` 一次），但会损害可复现性——awk `for (mod in mods)` 的输出顺序未指定，导致 run.ys 命令顺序不稳定，进而可能让派生指纹或中间产物出现无意义的抖动。`sort` 锁定了字母序。

---

### 4.3 RTLIL 模块过滤：剥离 `\` + 大写字母的「待确认模块」

#### 4.3.1 概念说明

到了 stage9，要把 RTLIL 转成 mapped JSON 交给 u5-l3 的报告脚本。但在转换前，本项目会先用 `filter_rtlil_modules.py` 剥离一类模块：**RTLIL 名字以 `\` 开头、且第二个字符是大写字母**的模块（如 `\Foo`），而 `\bar`、`main` 这类不在剥离之列。

这类「`\` + 大写」名字来自上游设计（CIRCT HW 降级保留了用户/源码层的 PascalCase 命名），在 3e 报告的语境里被项目当作「**待确认模块**」——它们的内部定义尚不应被展开计入资源账单。剥离其 `module ... end` 定义后，这些模块在后续 `proc` / `write_json` 里就变成了**未定义的引用**，Yosys 会把它们当作 blackbox 处理：JSON 里保留模块名，但没有内部叶单元。于是 u5-l3 的 `leaf_counts` 递归到这些 blackbox 时就停住、计为零，不污染资源统计。

#### 4.3.2 核心流程

`filter_rtlil_modules.py` 是一个**纯文本行扫描器**，逐行处理 RTLIL：

```text
对每一行：
  若匹配 ^module <名字>：
    判断 <名字> 是否该丢（should_drop：以 \ 开头且第二字符大写）
    若该丢：进入 dropping 状态，跳过本行（不写输出）
    若不该丢：正常写出
  否则（在某个 module 内部）：
    若处于 dropping 状态：
      若该行是 'end\n'：结束 dropping，跳过本行
      否则：跳过本行（即整块 module 定义都被丢弃）
    否则：正常写出
```

效果：整块 `module \Foo ... end` 被移除；其它模块原样保留。脚本要求必须显式传 `--drop-escaped-uppercase-modules`，否则直接 `SystemExit` 报错——这是一种**显式 opt-in 的安全门**，避免误用。

随后 stage9 的 `mkSynthStageJson` 拿过滤后的 `stage8-stripped.il` 跑 `proc` + `write_json` 出最终 JSON。

#### 4.3.3 源码精读

[filter_rtlil_modules.py:11](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/filter_rtlil_modules.py#L11) — `MODULE_RE` 只抓 `module ` 后的第一个非空白 token，即模块名（含前导 `\`）。

[filter_rtlil_modules.py:36-42](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/filter_rtlil_modules.py#L36-L42) — `should_drop` 三个条件：以 `\` 开头、长度 > 1、第二个字符 `isupper()`。三者同时满足才丢。

[filter_rtlil_modules.py:44-61](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/filter_rtlil_modules.py#L44-L61) — 主循环。注意两个细节：①遇到 `module` 行先决定 `dropping`，并 `continue`（不写该行），从而连 `module \Foo` 这一行本身也不进输出；②在 dropping 状态下，只有遇到恰好等于 `"end\n"` 的行才结束 dropping，跳过模块体的所有内容。

再看它在 stage9 的调用：

[flake.nix:353-384](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L353-L384) — `mkSynthStageJson`：先用 `filter_rtlil_modules.py --drop-escaped-uppercase-modules` 把 stage8 的 RTLIL 过滤成 `stage8-stripped.il`，再 `read_rtlil stage8-stripped.il` + `proc` + `write_json $out`。注意这一步**不带 `-top`、不做 hierarchy**，只做最小化的 proc + JSON 导出。

#### 4.3.4 代码实践

**实践目标**：精确预测 `should_drop` 在各种模块名上的行为。

**操作步骤**：

1. 阅读 [filter_rtlil_modules.py:36-42](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/filter_rtlil_modules.py#L36-L42)。
2. 对下列 4 个模块名判断是否会被剥离，并说明理由：
   - `\Foo`（`\` + 大写 F）
   - `\handshake_memory0`（`\` + 小写 h）
   - `main`（无 `\`）
   - `\$paramod$abc`（实际 RTLIL 里 `$` 开头的内部名不会以 `\` 开头，这里用来对比）
3. 用一句话说明：为什么剥离定义后，u5-l3 的资源报告不会被这些模块「虚高」或「虚低」影响？

**需要观察的现象**：只有 `\Foo` 被丢；其余保留。

**预期结果**：剥离后 `\Foo` 成为 blackbox，`leaf_counts` 递归到它时无叶单元可计，贡献为零；既不虚高（没把它的内部展开成海量 FF/LUT），也由于它是「待确认」模块而不应计入，因此符合预期。

**待本地验证**：可写一段迷你 RTLIL 跑 `python3 filter_rtlil_modules.py --input ... --output ... --drop-escaped-uppercase-modules` 验证。

#### 4.3.5 小练习与答案

**练习 1**：如果某个被剥离的 `\Foo` 模块其实仍被 `main` 实例化，`write_json` 会失败吗？

> **答案**：不会失败。`write_json`（以及 `proc`）允许存在未定义模块——它们被当作 blackbox 写进 JSON（只有端口、没有内部）。这正合本项目意图：让「待确认模块」以 blackbox 形式存在，不计入叶单元。

**练习 2**：为什么脚本在未传 `--drop-escaped-uppercase-modules` 时直接 `SystemExit`？

> **答案**：见 [filter_rtlil_modules.py:31-34](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/filter_rtlil_modules.py#L31-L34)。当前脚本唯一支持的过滤策略就是剥离大写 escaped 模块；不传该参数等于「什么都不做却仍然产出文件」，容易让人误以为做了过滤。显式 fail-fast 是为了避免这种静默无操作。

---

## 5. 综合实践

**任务**：完成规格要求的两件事——(A) 写出 stage1–stage9 每个阶段对应的 `synth_xilinx -run` 区间或命令；(B) 说明 stage4 的 `memory_map` 为什么要先 `awk` 找出含 `cell $mem` 的模块。

### (A) stage1–stage9 对应命令表

| 阶段 | 模具 | 关键命令（来自源码） |
| --- | --- | --- |
| stage1 | `mkSynthStageIl` | `proc`；`synth_xilinx -family xc7 -top <top> -noiopad -run begin:prepare` |
| stage2 | `mkSynthStageIl` | `synth_xilinx ... -run coarse:map_memory` |
| stage3 | `mkSynthStageIl` | `opt -fast -full`（自定义插入） |
| stage4 | `mkSynthStageMemoryMapIl` | `awk` 找含 `cell $mem` 的模块 → 逐个 `cd <mod>; memory_map; cd ..` |
| stage5 | `mkSynthStageIl` | `synth_xilinx ... -run fine:fine` |
| stage6 | `mkSynthStageIl` | `synth_xilinx ... -run map_cells:map_cells` |
| stage7 | `mkSynthStageIl` | `synth_xilinx ... -run map_ffs:map_ffs` |
| stage8 | `mkSynthStageIl` | `synth_xilinx ... -run map_luts:check` |
| stage9 | `mkSynthStageJson` | `filter_rtlil_modules.py --drop-escaped-uppercase-modules` → `proc` → `write_json` |

依据：[flake.nix:386-472](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L386-L472)。

### (B) stage4 为什么要先 `awk` 找含 `cell $mem` 的模块

1. **`memory_map` 的作用域是「当前模块」**：Yosys 的 `memory_map` 默认只展开当前 `cd` 进入的模块里的 `$mem` 单元。要让「定向」生效，必须先知道**哪些模块含 `$mem`**，再逐个 `cd` 进去执行。
2. **`$mem` 才是真正的内存实例化**：RTLIL 里 `memory \mem` 只是声明，`cell $mem` 才是实例化。用 `cell $mem` 作为判据，能精确锁定「确有待展开内存」的模块，避免对没有内存的模块做无意义的 `cd` / `memory_map`。
3. **替换 `synth_xilinx` 的一刀切内存处理**：进入综合前超大 Handshake 存储已被外部化成 blackbox，剩下的中小内存项目希望用可控的 `memory_map` 展开（而非让 `synth_xilinx` 的 `map_memory` 段全权决定）。把综合「断开」在 stage3 之后、stage5 之前，正是为了在 stage4 这个断点插自己的定向逻辑。
4. **可复现**：`awk` 输出经 `sort` 后再 `while read`，保证命令顺序确定、派生可复现。

依据：[flake.nix:309-351](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/flake.nix#L309-L351)。

**延伸观察（待本地验证）**：构建 `.#tiny-stories-1m-baseline-float-selftest-all-memory-utilization` 后，对照 3e 报告（[deliverables/3e-tiny-stories-1m-resource-report.md:42-46](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/deliverables/3e-tiny-stories-1m-resource-report.md#L42-L46)）确认这条「staged Yosys/Xilinx mapping」确实是「externalizes oversized Handshake memory → 分阶段综合 → 写 JSON → 出资源数」这整条链的中间环节。

## 6. 本讲小结

- `synth_xilinx` 是一条带标签（`begin`/`prepare`/`coarse`/`map_memory`/`fine`/`map_cells`/`map_ffs`/`map_luts`/`check`）的长脚本，`-run <from>:<to>` 可只执行其中一段；`X:X` 隔离单段、`A:B` 跨多段。
- `mkSynthJsonStages` 把综合拆成 9 个独立 Nix 派生（8 个出 RTLIL、1 个出 JSON），阶段间唯一耦合是「上一阶段 `$out` = 下一阶段输入」，因此各自可缓存、可观察。
- 只有 stage1 带 `topSv`（合并自测外壳 SV）且带 `preCommands=[proc]`；stage3 是自定义插入的 `opt -fast -full`——这两处体现了「拆阶段」带来的灵活性。
- stage4 的 **targeted memory_map** 用 `awk` 先挑出含 `cell $mem` 的模块，再逐个 `cd` / `memory_map` / `cd ..`，以定向、可控的方式替换 `synth_xilinx` 的一刀切内存处理。
- stage9 前的 `filter_rtlil_modules.py` 用 `--drop-escaped-uppercase-modules` 剥离「`\` + 大写字母」的待确认模块定义，使其在 `write_json` 时退化为 blackbox，不把内部展开计入资源账单。
- 这条分阶段链只服务于 TinyStories 大设计（`mkTinyStoriesSelftestBundle`）；matmul 用 u5-l2 的一体化 `mkSynthJson`。

## 7. 下一步学习建议

- **向上游追**：读 u6-l2（外部化超大 Handshake 存储），理解 stage4 之前那些超大 `$mem` 是如何被 blackbox 成外部存储候选的，从而明白 stage4 的 `memory_map` 只剩下中小型内存可展开。
- **向下游接**：回看 u5-l3 的 `write_utilization_report.py`，确认 stage9 产出的 mapped JSON 是如何被 `leaf_counts` 递归拍平、并与 `fpgaCapacities` 对比得到「约 141 倍超配」结论的。
- **补丁栈视角**：读 u6-l4（CIRCT 补丁栈与瓶颈结论），理解为什么「换掉 Handshake 方言」是 Task 6 的目标——这与本讲 stage4 因 Handshake 存储而带来的内存处理负担一脉相承。
- **动手实验**：在 `mkSynthJsonStages` 里临时插一个 stage（例如在 stage7 后再追加一条 `opt -full`），观察 Nix 只重算该阶段及其下游，亲手验证「分阶段 = 可缓存」的工程价值。
