# HW 到 SystemVerilog 导出与 FP extern 处理

## 1. 本讲目标

本讲是 CIRCT 后端降级链的**终点**：把上一讲（u3-l3）产出的干净 `hw-clean.mlir`（CIRCT 的 `hw` 硬件方言）翻译成可以被 Yosys 读懂的 **SystemVerilog 文本**。

学完后你应该能够：

- 说清楚 `circt-opt` 用哪几条 `-lower-*` pass 把 `seq`/`hw` 方言降到 SystemVerilog，以及它们的顺序为什么是这样。
- 解释 `--export-split-verilog` 如何把一个大 MLIR 拆成「一个 `hw.module` 一个 `.sv` 文件」，并理解 `sources.f` 与 `main.sv` 的作用。
- 看懂脚本里那道**「禁止裸 extern」安全门**：它如何用 `grep` 检测 `hw.module.extern`，在没有显式授权时直接报错退出。
- 理解浮点（FP）extern 是从哪里来的（CIRCT 补丁），以及 `circt_fp_primitives.sv` 如何用 **Q16.16 定点近似**给这些 extern 提供可综合实现，并被「挂接」进输出目录。

本讲对应流水线脚本 `hw_clean_to_sv.sh`，是降级链里**唯一既做降级、又做安全检查、还要拼接外部文件**的一站。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 CIRCT 的硬件表示：`hw` 方言与 `seq` 方言

CIRCT 用两个 MLIR 方言来描述硬件：

- **`hw` 方言**：表示结构化的硬件——模块（`hw.module`）、实例化（`hw.instance`）、连线、`hw.struct` 类型等。它接近「网表」的概念，是**组合的、没有时钟概念**。
- **`seq` 方言**：表示**时序**元件——寄存器（`seq.compreg`）、存储、FIFO、移位寄存器等。`seq` 的特点是「带隐式时钟」：编译器知道某个寄存器接哪个时钟，但**先不写** `always_ff`，等到降级阶段再展开成具体的 SystemVerilog。

上一讲 u3-l3 产出的 `hw-clean.mlir` 里 `hw` 和 `seq` 两种方言并存。本讲的任务就是用一组 pass 把它们**统统降成 SystemVerilog 文本**。

### 2.2 什么是 `extern`（黑盒）

`hw.module.extern` 是一个**只有声明、没有实现**的模块——也就是硬件圈说的「黑盒」（blackbox）。它告诉编译器「这个模块存在，端口长这样，但我不告诉你它内部怎么实现」。

这本身不是坏事：很多 IP 核、外部存储、外部浮点单元都天然是黑盒。**危险的是「意外的裸 extern」**——如果降级过程中某个算子没被正常降级，意外留下一个没人提供实现的 extern，那么到了 Yosys 综合阶段就会报 `Module not found`，整条链崩掉。本讲脚本的核心价值之一，就是在 SV 导出这一站**提前拦住**这种意外。

### 2.3 一个模块一个文件

Yosys 综合时通常一次读入一组 `.sv` 文件。CIRCT 提供的 `--export-split-verilog` 会按 `hw.module` 把 IR 拆开，**每个模块写成一个独立 `.sv` 文件**放到指定目录，并约定顶层模块叫 `main.sv`。这种拆分让下游工具可以按需引用，也让 `sources.f`（文件清单）成为一种稳定的接口。

> 术语速查：**降级（lowering）** = 把高层表示翻译成更底层表示的一组变换（pass）；**方言（dialect）** = MLIR 里某一层的「语言」，如 `hw`/`seq`/`linalg`；**pass** = 一条具体的变换命令；**黑盒/extern** = 只有端口没有实现的模块。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [hw_clean_to_sv.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh) | **本讲主角**。读入 `hw-clean.mlir`，做 extern 安全检查，降级到 SV，拆分输出，挂接 FP 实现。 |
| [rtl/fp/circt_fp_primitives.sv](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv) | 给浮点 extern 提供的 **Q16.16 定点近似**可综合实现（`arith_*`、`math_*` 一系列模块）。 |
| [common.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/common.sh) | 脚手架：`require_file` / `require_executable`（u2-l3 已讲）。 |
| [nix/pipeline.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix) | `mkSvDerivation` 把本脚本包成可缓存派生，并注入 `ALLOW_HW_EXTERNS` / `FP_PRIMS_SV` 两个环境变量。 |
| [nix/models.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix) | 只有 `tiny-stories-1m-baseline-float` 打开了 `allowHwExterns` 并带入 `fpPrimsSv`；`matmul` 没有。 |
| [patches/circt-task3-rfp/0015-...patch](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch) | CIRCT 补丁：让浮点算子以 extern 形式降级——这正是 FP extern 的**来源**。 |

## 4. 核心概念与源码讲解

### 4.1 seq/hw 到 SystemVerilog 的最后降级

#### 4.1.1 概念说明

把 `hw` + `seq` 方言变成 SystemVerilog，不是一步到位的，而是**按元件类别逐类展开**。`seq` 方言里藏着好几类时序元件：高阶层存储（hlmem）、FIFO、移位寄存器、普通寄存器。它们各自有一条专门的 `-lower-seq-*` pass 负责展开成 SV 的 `always_ff` 块或连续赋值；最后再用一条 `-lower-hw-to-sv` 把残留的 `hw` 方言结构（如 `hw.struct` 类型）也落到 SV。

这套顺序是 CIRCT 官方推荐的「seq 先降、hw 后降、中间穿插 canonicalize/cse 清理」的标准做法。本项目 `hw_clean_to_sv.sh` 几乎原样照搬。

#### 4.1.2 核心流程

```
hw-clean.mlir (hw + seq 方言)
        │
        ├── -lower-seq-hlmem      # 高层存储 → 显式存储结构
        ├── -lower-seq-fifo       # FIFO → SV
        ├── -lower-seq-shiftreg   # 移位寄存器 → SV
        ├── -lower-seq-to-sv      # 其余 seq（如寄存器）→ SV always_ff
        ├── -canonicalize / -cse  # 清理
        ├── -lower-hw-to-sv       # hw 方言残留（struct 等）→ SV
        ├── -canonicalize / -cse  # 再清理
        │
        └── --export-split-verilog="dir-name=$output_dir/sv"
                    # 每个 hw.module 写成一个 .sv 文件
```

可以把它看作三组：**① seq 降级（4 条）→ ② 清理 → ③ hw 降级 → ④ 清理 → ⑤ 拆分导出**。

#### 4.1.3 源码精读

降级与导出的核心在脚本第 60–72 行：

[hw_clean_to_sv.sh:60-72](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L60-L72) — 这段先 `mkdir` 建好输出目录，再调用 `circt-opt` 一次跑完整组 pass，最后用 `--export-split-verilog` 把每个模块拆成单独 `.sv` 文件。

```bash
mkdir -p "$output_dir/sv"
"$circt_opt" "$input" \
  -lower-seq-hlmem \
  -lower-seq-fifo \
  -lower-seq-shiftreg \
  -lower-seq-to-sv \
  -canonicalize \
  -cse \
  -lower-hw-to-sv \
  -canonicalize \
  -cse \
  --export-split-verilog="dir-name=$output_dir/sv" \
  -o /dev/null
```

几个要点：

- `-lower-seq-hlmem` 里的 **hlmem = high-level memory**（高层存储）。这些存储很多是从上一讲 Handshake 降级时落地的大存储（如 matmul 的 `handshake_memory0`）带过来的，需要先从 `seq` 的高层存储表示降成更接近 SV 的形式。
- `-lower-seq-to-sv` 是 seq 降级的**兜底 pass**：前面三条处理掉存储/FIFO/移位寄存器后，剩下的普通寄存器（`seq.compreg`）由它统一写成 SV 的 `always_ff` 块。
- 中间两组 `-canonicalize` + `-cse`（公共子表达式消除）用来清理降级产生的冗余，让最终 SV 更干净。
- `-lower-hw-to-sv` 处理 `hw` 方言里还没落到 SV 的部分（例如 `hw.struct` 类型解包成位向量）。
- `--export-split-verilog` 是**带副作用**的 pass：它不写到 stdout，而是直接往 `dir-name` 指定的目录里写一批 `.sv` 文件。因此末尾 `-o /dev/null` 把 circt-opt 默认的单文件 stdout 输出丢弃——我们要的是拆分后的目录，不是单文件。

#### 4.1.4 代码实践

**实践目标**：亲眼看到每条 `-lower-seq-*` pass 分别处理一类元件。

**操作步骤**（源码阅读型，可选本地运行）：

1. 读 [hw_clean_to_sv.sh:61-71](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L61-L71)，把 9 条 pass 按「seq 降级 / 清理 / hw 降级 / 拆分」分成 4 组。
2. （可选，需 `nix develop`）拿到一个 `hw-clean.mlir` 后，**逐步删减** pass 重跑 circt-opt，例如只保留前 4 条 seq pass，观察输出里是否还残留 `hw.struct` 或 `seq.compreg`：

   ```bash
   circt-opt hw-clean.mlir -lower-seq-hlmem -lower-seq-fifo \
       -lower-seq-shiftreg -lower-seq-to-sv | grep -E 'seq\.|hw\.struct'
   ```

**需要观察的现象**：只跑 seq pass 时，`seq.` 元件应该消失，但 `hw.struct` 之类仍可能存在；只有再跑 `-lower-hw-to-sv` 后，`hw` 方言特有的结构才被清掉。

**预期结果**：4 条 seq pass 负责「时序 → SV」，`-lower-hw-to-sv` 负责「组合结构 → SV」，二者分工明确、缺一不可。本地实际输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `-lower-seq-*` 四条 pass 必须在 `-lower-hw-to-sv` **之前**跑？
**参考答案**：`seq` 元件（寄存器、存储）降级后会产生新的 `hw` 级连线与结构；先降 seq 再降 hw，才能保证 `-lower-hw-to-sv` 看到、并处理掉全部 `hw` 残留。反过来先降 hw，seq 展开时新冒出来的 hw 结构就没人清理了。

**练习 2**：末尾的 `-o /dev/null` 能不能删掉？为什么？
**参考答案**：不能简单删掉。`--export-split-verilog` 把输出写进目录（副作用），circt-opt 默认还会往 stdout 打一份单文件 SV；`-o /dev/null` 就是把这份默认输出丢进黑洞，避免它污染脚本 stdout。删掉后 stdout 会出现整份 SV 文本（在本脚本的 `runCommand` 场景里属于无害噪音，但不符合「只通过目录交付产物」的设计）。

---

### 4.2 export-split-verilog 拆分与 sources.f

#### 4.2.1 概念说明

一个 `hw-clean.mlir` 里通常有**很多个** `hw.module`（顶层 `main` 加上一堆被实例化的子模块）。`--export-split-verilog` 的职责是「**一个模块一个文件**」地写到目录里，而不是堆成一个大文件。这样下游 Yosys 可以拿到一个稳定的**文件清单** `sources.f`，按清单读入即可。

本讲脚本在导出后还做两件小事：**断言顶层 `main.sv` 存在**，以及**生成 `sources.f`**。这两样构成了本站交给下一站（`sv_to_il.sh`）的标准接口。

#### 4.2.2 核心流程

```
--export-split-verilog 写出:
   $output_dir/sv/main.sv        # 顶层 hw.module @main
   $output_dir/sv/<其它模块>.sv   # 各子模块
        │
        ├── require_file main.sv   # 必须有顶层，否则报错退出码 2
        └── find *.sv | sort > sources.f   # 生成文件清单
```

#### 4.2.3 源码精读

[hw_clean_to_sv.sh:71-76](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L71-L76) — 拆分导出、断言顶层、生成清单三步连在一起：

```bash
  --export-split-verilog="dir-name=$output_dir/sv" \
  -o /dev/null

require_file "$output_dir/sv/main.sv"

find "$output_dir/sv" -type f -name '*.sv' | sort >"$output_dir/sources.f"
```

- `require_file`（来自 common.sh，u2-l3 讲过）在找不到 `main.sv` 时走 stderr 报错、退出码 2。这一步是**拓扑正确性的兜底**：如果降级链忘了把顶层命名为 `@main`、或导出器没产出顶层，这里立刻熔断，而不是把一个残缺产物甩给 Yosys。
- `find ... -name '*.sv' | sort` 把目录下所有 `.sv` 排好序写进 `sources.f`。排序是为了让清单**确定性可复现**（Nix 要求派生输出与文件顺序无关地稳定）。

> 注意：此刻 `sources.f` 还**不包含** FP primitives 文件——那个文件在 4.4 讲的挂接步骤里才会被追加进去（而且那时它还没被拷贝到目录里，所以这一行的 `find` 也搜不到它）。

#### 4.2.4 代码实践

**实践目标**：理解 `sources.f` 这个「文件清单」接口如何被下游消费。

**操作步骤**：

1. 读 [hw_clean_to_sv.sh:74-76](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L74-L76) 与下一站 [sv_to_il.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/sv_to_il.sh)（或 [nix/pipeline.nix:86-95](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L86-L95) 的 `mkIlDerivation`）。
2. 注意 `mkIlDerivation` 把 `${sv}/sources.f` 作为第三个参数传给 `sv_to_il.sh`——下一站 Yosys 正是按这份清单逐文件读 SV。

**需要观察的现象**：本站产出的 `${name}-sv/` 派生里同时包含 `sv/*.sv`、`sv/main.sv` 和顶层 `sources.f`。

**预期结果**：`sources.f` 是本站与下一站之间的**契约文件**——它把「目录里有哪几个 SV、按什么顺序读」固化下来，使 Yosys 不必自己去 glob。

#### 4.2.5 小练习与答案

**练习 1**：假设某次降级意外没产出 `main.sv`，脚本会在哪一行、以什么退出码失败？
**参考答案**：在 [hw_clean_to_sv.sh:74](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L74) 的 `require_file "$output_dir/sv/main.sv"` 处失败，`require_file` 走 stderr 报 `missing input file: ...`，退出码为 **2**。

**练习 2**：为什么 `find` 之后要接 `sort`？
**参考答案**：文件系统 `readdir` 的顺序不保证确定；`sort` 让 `sources.f` 在不同机器、不同次构建里**字节一致**，这是 Nix 可复现派生的硬性要求，也让下游 Yosys 读入顺序稳定。

---

### 4.3 FP extern 检测与「禁止裸 extern」安全门

#### 4.3.1 概念说明

这一节是**本讲脚本真正的灵魂**。正如前置知识所说，「意外的裸 extern」是降级链最危险的失败模式之一——一个没人实现的黑盒模块混进 SV，会让下游 Yosys 在综合时才报 `Module not found`，错误信息离根因很远、很难调。

`hw_clean_to_sv.sh` 因此设计成一道**显式的安全门**：

1. 先用 `grep` 把输入 MLIR 里所有 `hw.module.extern @名字` 扫出来。
2. **默认拒绝**：只要发现了 extern，而环境变量 `ALLOW_HW_EXTERNS` 不是 `1`，立即报错退出——一个 extern 都不许漏到 SV。
3. **白名单放行**：如果 `ALLOW_HW_EXTERNS=1`，还必须提供 `FP_PRIMS_SV`（一个能覆盖所有 extern 的实现文件）。
4. **逐个核对**：对每个 extern 名字，去 `FP_PRIMS_SV` 里找有没有对应实现；任何一个找不到，都报「缺失实现」并退出。

这套「默认拒绝 + 显式放行 + 逐个核对」是处理黑盒的标准工程范式。

#### 4.3.2 核心流程

```
1. grep hw.module.extern → 提取所有 extern 名字 → sort -u 去重
        │
        ├── 无 extern  → 直接进入降级（正常路径，如 matmul）
        │
        └── 有 extern  → 检查 ALLOW_HW_EXTERNS
                ├── ≠ 1        → 报错 exit 1（禁止裸 extern）
                └── = 1        → 检查 FP_PRIMS_SV
                        ├── 未设/不存在 → 报错 exit 1
                        └── 存在 → 逐个 extern 在 FP_PRIMS_SV 里找实现
                                ├── 任一缺失 → 报错 exit 1（列出缺失项）
                                └── 全部命中 → 进入降级
```

#### 4.3.3 源码精读

**第一步：提取 extern 名字**，[hw_clean_to_sv.sh:22-24](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L22-L24)：

```bash
grep -oE 'hw\.module\.extern[[:space:]]+@([A-Za-z_][A-Za-z0-9_]*)' "$input" \
  | sed -E 's/.*@([A-Za-z_][A-Za-z0-9_]*).*/\1/' \
  | sort -u >"$tmp_externs" || true
```

- `grep -oE` 只输出匹配片段：抓 `hw.module.extern @xxx`，`xxx` 是 extern 的符号名。
- `sed` 把 `@xxx` 里的名字抠出来。
- `sort -u` 去重。
- 结尾的 `|| true` 很关键：`grep` 在**一个匹配都没有**时返回退出码 1，而脚本开头 `set -euo pipefail` 会让整个脚本因此退出；`|| true` 把「没有 extern」（matmul 的正常情况）也当作成功，避免误杀。

**第二步：默认拒绝裸 extern**，[hw_clean_to_sv.sh:26-32](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L26-L32)：

```bash
if [[ -s "$tmp_externs" ]]; then
  if [[ "${ALLOW_HW_EXTERNS:-0}" != "1" ]]; then
    echo "[hw_clean_to_sv] ERROR: extern modules found in '$input'." >&2
    echo "[hw_clean_to_sv] Eliminate hw.module.extern before SV export." >&2
    cat "$tmp_externs" >&2
    exit 1
  fi
```

`-s` 判断文件非空（即有 extern）。一旦发现 extern 且 `ALLOW_HW_EXTERNS` 未设为 1，就把所有 extern 名字打到 stderr，**退出码 1**。

**第三步：要求 FP_PRIMS_SV**，[hw_clean_to_sv.sh:34-40](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L34-L40)：放行（`ALLOW_HW_EXTERNS=1`）时，还必须有 `FP_PRIMS_SV` 指向一个真实存在的文件，否则同样 `exit 1`。

**第四步：逐个核对实现**，[hw_clean_to_sv.sh:42-57](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L42-L57)：

```bash
while IFS= read -r mod; do
  has_impl_cmd=(
    grep -nE
    '(^module[[:space:]]+'"${mod}"'\b|^`[A-Za-z_][A-Za-z0-9_]*[[:space:]]*\([[:space:]]*'"${mod}"'\b)'
    "$FP_PRIMS_SV"
  )
  if ! "${has_impl_cmd[@]}" >/dev/null 2>&1; then
    echo "$mod" >>"$tmp_missing"
  fi
done <"$tmp_externs"
if [[ -s "$tmp_missing" ]]; then
  echo "[hw_clean_to_sv] ERROR: FP_PRIMS_SV does not define all extern modules." >&2
  cat "$tmp_missing" >&2
  exit 1
fi
```

对每个 extern 模块名 `mod`，到 `FP_PRIMS_SV` 里 grep 两种声明形式：一是顶层的 `module <mod>`（当前 `circt_fp_primitives.sv` 用的就是这种），二是反引号宏调用形式 `` `Macro(<mod> ``（一种备用的、以宏展开定义的写法）。任何在 `FP_PRIMS_SV` 里**找不到实现**的 extern，都会被收进 `tmp_missing`，最后一次性报出并 `exit 1`。

#### 4.3.4 代码实践（本讲指定任务）

**实践目标**：讲清楚两件事——① 当 `ALLOW_HW_EXTERNS` 未设、但输入含 extern 时会发生什么；② 当 `FP_PRIMS_SV` 缺失某个 extern 实现时，报错走的是哪条路径。

**操作步骤**：

1. 读 [hw_clean_to_sv.sh:22-58](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L22-L58)，对照下面的答案。
2. 可选：在 `nix develop` 里手工构造一个只含 `hw.module.extern @foo(...)` 的 MLIR，跑 `ALLOW_HW_EXTERNS` 不设的情况，观察 stderr。

**问题①答案**：当输入含 extern、但 `ALLOW_HW_EXTERNS` 未设（或不是 `1`）时，脚本在第 27–32 行被拦下——打印两行 `ERROR`，`cat` 出所有 extern 名字，然后 **`exit 1`**。脚本**不会**进入第 60 行的 circt-opt 降级，SV 也不会被导出。也就是说，extern 在这一站就被「熔断」，根本到不了 Yosys。

**问题②答案**：当 `ALLOW_HW_EXTERNS=1` 且 `FP_PRIMS_SV` 已设、但其中缺了某个 extern（比如 CIRCT 产生了 `math_absf_...` 而 `.sv` 文件里没定义）时，第 42–57 行的 `while` 循环会把缺失项写进 `$tmp_missing`；循环结束后第 53–57 行判断 `$tmp_missing` 非空，打印 `ERROR: FP_PRIMS_SV does not define all extern modules.` 并 `cat` 出缺失模块名，最后 **`exit 1`**。报错信息会**明确列出**到底是哪几个 extern 没有实现，方便定位。

**预期结果**：两种情形都以 `exit 1` 终止、不产出 SV，并把诊断信息打到 stderr。这正是「禁止裸 extern」安全门的全部行为。

#### 4.3.5 小练习与答案

**练习 1**：为什么提取 extern 的那行末尾要加 `|| true`？如果去掉会发生什么？
**参考答案**：`grep` 在无匹配时退出码为 1，配合 `set -euo pipefail` 会让整行失败、脚本退出。matmul 这类**根本没有 extern** 的正常输入会因此被误判为错误。`|| true` 保证「无 extern」是合法结果。去掉后，所有像 matmul 这样无 extern 的模型都会在导出前无谓失败。

**练习 2**：核对实现用的 grep 为什么接受「`module <mod>`」和「`` `Macro(<mod> ``」两种形式？
**参考答案**：第一种 `module <mod>` 是 SystemVerilog 标准的模块声明，当前 `circt_fp_primitives.sv` 全部使用这种。第二种反引号宏形式是**备用**：允许将来用编译器宏（`` `define ``）来展开模块定义。同时接受两种，让实现文件可以选择声明风格而不必改检测逻辑。

---

### 4.4 FP extern 的挂接：circt_fp_primitives.sv 的定点近似实现

#### 4.4.1 概念说明

先回答一个关键问题：**这些 FP extern 是从哪儿来的？**

CIRCT 上游的 Handshake→HW 降级，本来不会把浮点算子变成黑盒。但本项目为了让 TinyStories-1M（含大量 `tanh`/`exp`/`rsqrt`/浮点乘加等）能跑通降级链，给 CIRCT 打了一组补丁。其中 [patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch) 把 `arith.addf/subf/mulf/divf/...`、`math.exp/tanh/rsqrt/fpowi/...` 等浮点算子统一改成**以 `hw.module.extern` 形式降级**：

```cpp
// Floating-point arith operations are lowered as extern modules.
ExtModuleConversionPattern<arith::AddFOp>,
ExtModuleConversionPattern<arith::MulFOp>,
ExtModuleConversionPattern<math::TanhOp>,
ExtModuleConversionPattern<math::RsqrtOp>,
...
```

这样降级后，MLIR 里就会出现一批名字被「mangle（绞碎编码）」过的 extern，命名约定形如：

```
<方言>_<算子>_in_<输入类型>_out_<输出类型>[_<谓词>]
```

例如 `arith_addf_in_f32_f32_out_f32`、`math_tanh_in_f32_out_f32`、`arith_cmpf_in_f32_f32_out_ui1_ogt`。名字把「算子 + 完整类型签名」编码进去，保证每种「算子×类型组合」对应唯一 extern。

**挂接**就是：在 4.3 的安全门放行之后，把 `circt_fp_primitives.sv` 这个**提供上述全部 extern 实现**的文件，拷进输出目录并追加到 `sources.f`，让下游 Yosys 能读到。

#### 4.4.2 核心流程

```
安全门放行（全部 extern 都有实现）
        │
        ├── cp $FP_PRIMS_SV  →  $output_dir/sv/zz_circt_fp_primitives.sv
        │        （zz_ 前缀让它在排序中靠后、名字独特）
        └── 把该路径 append 进 sources.f
              → 下游 Yosys 读 sources.f 时一并读入这些实现
```

而 `circt_fp_primitives.sv` 内部用的是一种**近似**策略：不实现真正的 IEEE-754 浮点 IP，而是把 f32/f64 **解码成 Q16.16 定点数**，在定点域里做运算，再**编码回 f32**。这是「精度换可综合性与面积」的典型工程取舍（详见 u6-l3）。

#### 4.4.3 源码精读

**挂接三行**，[hw_clean_to_sv.sh:78-82](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L78-L82)：

```bash
if [[ -s "$tmp_externs" ]]; then
  fp_sv="$output_dir/sv/zz_circt_fp_primitives.sv"
  cp "$FP_PRIMS_SV" "$fp_sv"
  printf '%s\n' "$fp_sv" >>"$output_dir/sources.f"
fi
```

- 只在**确实有 extern** 时才挂接（`-s` 判断）；matmul 这类无 extern 的模型不会带这个文件。
- 拷贝时改名为 `zz_circt_fp_primitives.sv`：`zz_` 前缀保证它排序靠后、与生成的模块文件名不冲突，且一眼可辨。
- 用 `>>` **追加**到 `sources.f`（注意 4.2 里的 `find` 已经先写好了生成文件清单，这里只追加这一条），下游 Yosys 读清单时就会一起读入这些 FP 实现。

**定点近似的核心：f32 → Q16.16**，[circt_fp_primitives.sv:22-41](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L22-L41)。这是整个近似方案的钥匙：

```systemverilog
function automatic logic signed [31:0] f32_to_q16_16(input logic [31:0] f);
  ...
  sign = f[31]; exp = f[30:23]; frac = f[22:0];
  ...
  if (exp == 8'h00) begin mant = {1'b0, frac}; e = -126; end
  else begin mant = {1'b1, frac}; e = $signed({1'b0, exp}) - 127; end
  shift = e - 23 + 16;
  scaled = $signed({40'b0, mant});
  scaled = (shift >= 0) ? (scaled <<< shift) : (scaled >>> (-shift));
  if (sign) scaled = -scaled;
  f32_to_q16_16 = sat32(scaled);
  ...
```

- `sign/exp/frac` 是把 32 位 f32 按符号位、8 位指数、23 位尾数拆开。
- `e` 是**去偏移**后的真实指数（`exp - 127`）。
- `mant` 是含隐含最高位的 24 位尾数（规格化数补上 `1`）。
- `shift = e - 23 + 16` 是把「整数尾数」对齐到 Q16.16（16 位小数）所需的移位量（4.4.4 会详解）。
- `sat32`（[circt_fp_primitives.sv:15-20](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L15-L20)）做**饱和截断**，把超出 32 位有符号范围的值钳到最大/最小，避免回绕。

**算子实现示例**，以加法为例，[circt_fp_primitives.sv:181-191](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L181-L191)：

```systemverilog
module arith_addf_in_f32_f32_out_f32 (
  input logic [31:0] in0, input logic in0_valid,
  input logic [31:0] in1, input logic in1_valid, input logic out0_ready,
  output logic in0_ready, output logic in1_ready,
  output logic [31:0] out0, output logic out0_valid
);
  import circt_fp_fixed_pkg::*;
  logic signed [31:0] a_q, b_q;
  assign a_q = f32_to_q16_16(in0); assign b_q = f32_to_q16_16(in1);
  assign out0 = q16_16_to_f32(sat32($signed(a_q) + $signed(b_q)));
  assign out0_valid = in0_valid & in1_valid;
  assign in0_ready = out0_ready & in1_valid;
  assign in1_ready = out0_ready & in0_valid;
endmodule
```

注意端口里那组 `valid`/`ready` 信号——这正是 u3-l2/u3-l3 讲的 **Handshake 弹性数据流接口**被一路带到 SV 的证据：每个 FP extern 模块都是一个独立的小握手节点，`out0_valid = in0_valid & in1_valid`、`in0_ready = out0_ready & in1_valid` 是典型的「两输入一输出」握手逻辑。模块名 `arith_addf_in_f32_f32_out_f32` 与 4.4.1 的 mangle 约定完全一致，所以 4.3 的 grep 能精确命中它。

#### 4.4.4 代码实践（本讲指定任务）

**实践目标**：解释 `f32_to_q16_16` 里 `shift = e - 23 + 16` 的含义，并讨论这种 Q16.16 近似对 LLM 推理精度的潜在影响。

**操作步骤**：

1. 读 [circt_fp_primitives.sv:22-41](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L22-L41)。
2. 对照下面的推导，确认 `shift` 三个分量的来源。

**关于 `shift = e - 23 + 16` 的推导**：

一个规格化 f32 的真实数值是

\[
\text{value} = \text{mant} \times 2^{e-23}
\]

其中 `mant` 是 24 位整数（含隐含的 `1.`，所以它的有效值是 `mant / 2^23`），`e` 是去偏移后的指数。要把这个值表示成 **Q16.16 定点**（即再乘以 \(2^{16}\)），需要：

\[
\text{q1616} = \text{value} \times 2^{16} = \text{mant} \times 2^{e-23} \times 2^{16} = \text{mant} \times 2^{e-23+16}
\]

所以 `shift = e - 23 + 16` 三个分量的含义是：

| 分量 | 含义 |
| --- | --- |
| `e` | 去偏移后的真实指数（`exp - 127`） |
| `- 23` | 把 24 位整数尾数折算回 `[1,2)` 小数（除以 \(2^{23}\)） |
| `+ 16` | Q16.16 的定点小数位（乘以 \(2^{16}\)） |

即：`shift` 是把「整数尾数」对齐到「Q16.16 定点整数」所需的**净移位量**。`shift >= 0` 左移放大，`shift < 0` 算术右移缩小。

**对 LLM 推理精度的影响（讨论）**：

- **表示范围 vs 精度**：Q16.16 只有 16 位整数 + 16 位小数。整数 16 位可表范围约 `[-32768, 32767]`，小数分辨率约 \(2^{-16} \approx 1.5\times10^{-5}\)。LLM 里出现的中间值（如注意力 logits、softmax 前的分数）很容易超出这个范围或需要更高精度，这时会被 `sat32` **饱和截断**或**量化舍入**，引入误差。
- **特殊函数近似**：`math_exp` 用 4 项泰勒级数、`math_tanh` 用有理近似、`math_rsqrt` 用 3 次牛顿迭代（见 [circt_fp_primitives.sv:105-149](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L105-L149)），都是**低阶近似**，在大输入或长序列上误差会累积。
- **深层模型放大误差**：TinyStories 是多层 Transformer，每层的定点量化误差会层层传播、叠加，最终可能显著改变输出 token 分布。
- **结论**：这套实现的首要目标是**让降级链能综合通过**（占位实现），而非保精度。它足以验证「流水线能跑通、能下板自测」，但离真实可用的推理精度还有距离——这正是 u6-l3 要深入讨论、Task 6 要优化的对象。本讨论属源码阅读型定性分析，**待本地验证**具体数值误差。

#### 4.4.5 小练习与答案

**练习 1**：为什么挂接时把文件改名为 `zz_circt_fp_primitives.sv`，而不是用原名？
**参考答案**：① `zz_` 前缀让它在字母排序中**靠后**，避免和生成的 `main.sv`、其它模块文件名冲突或混淆；② 给这个「外部拼进来的实现文件」一个**独特、可辨**的名字，让人一眼看出它不是 CIRCT 自动生成的；③ 追加进 `sources.f` 后顺序确定，下游 Yosys 读入行为可复现。

**练习 2**：matmul 模型会挂接这个 FP 文件吗？为什么？
**参考答案**：不会。matmul 是纯整数的 `torch.aten.matmul`，降级链里不会产生任何浮点 extern，所以 [hw_clean_to_sv.sh:78](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L78) 的 `if [[ -s "$tmp_externs" ]]` 为假，整段挂接被跳过；`models.nix` 里 matmul 也没设 `allowHwExterns`/`fpPrimsSv`。只有 `tiny-stories-1m-baseline-float` 才会触发。

## 5. 综合实践

把本讲的「降级 + 安全门 + 挂接」串起来做一次**纸面端到端跟踪**。

**任务**：假设你拿到一个 `hw-clean.mlir`，里面顶层是 `hw.module @main`，并含两个 extern：

```
hw.module.extern @arith_mulf_in_f32_f32_out_f32(...)
hw.module.extern @math_tanh_in_f32_out_f32(...)
```

请按 `hw_clean_to_sv.sh` 的逻辑，回答以下问题并写出每个判断点的行号：

1. 第 22–24 行的 grep 会把哪两个名字写进 `$tmp_externs`？
2. 若环境是 `ALLOW_HW_EXTERNS` 未设，脚本会在哪一行、以什么退出码终止？会不会产出 `main.sv`？
3. 若环境是 `ALLOW_HW_EXTERNS=1` 且 `FP_PRIMS_SV=rtl/fp/circt_fp_primitives.sv`，第 42–57 行会分别去 `.sv` 文件里 grep 哪两个模式？这两个 extern 在 [circt_fp_primitives.sv](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv) 里是否都有实现（给出对应行号）？
4. 全部放行后，挂接步骤（78–82 行）会把文件拷成什么名字、追加到哪个清单文件？

**参考答案**：

1. `arith_mulf_in_f32_f32_out_f32` 与 `math_tanh_in_f32_out_f32`（去重后）。
2. 在第 27–32 行终止，**退出码 1**，把这两个名字打到 stderr；**不会**进入第 60 行的 circt-opt，所以**不产出** `main.sv`。
3. 分别 grep `^module arith_mulf_in_f32_f32_out_f32\b` 与 `^module math_tanh_in_f32_out_f32\b`（及其宏形式）。两者在 `circt_fp_primitives.sv` 里都有：乘法在 [第 205-215 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L205-L215)，tanh 在 [第 356-364 行](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L356-L364)。`$tmp_missing` 为空，放行。
4. 拷成 `$output_dir/sv/zz_circt_fp_primitives.sv`，并把该路径 `>>` 追加到 `$output_dir/sources.f`。

## 6. 本讲小结

- 本讲把 u3-l3 产出的 `hw-clean.mlir`（`hw`+`seq` 方言）经 `circt-opt` 一组 pass **降到 SystemVerilog 文本**，并用 `--export-split-verilog` **按模块拆成多个 `.sv` 文件**。
- 降级 pass 分两组：`-lower-seq-hlmem/fifo/shiftreg/-lower-seq-to-sv` 展开**时序元件**，`-lower-hw-to-sv` 展开**组合结构**，中间穿插 `-canonicalize`/`-cse` 清理。
- 脚本是一道**「禁止裸 extern」安全门**：先 grep 出所有 `hw.module.extern`，默认拒绝；只有 `ALLOW_HW_EXTERNS=1` 且 `FP_PRIMS_SV` 覆盖全部 extern 时才放行，否则 `exit 1` 并列出问题模块。
- FP extern 来源于 CIRCT 补丁（0015）把浮点算子以 mangle 命名的黑盒形式降级；`circt_fp_primitives.sv` 用 **Q16.16 定点近似**给它们提供可综合实现。
- 挂接 = 把 FP 实现拷成 `zz_circt_fp_primitives.sv` 并追加进 `sources.f`，让下游 Yosys 读到；`sources.f` 与必有的 `main.sv` 一起构成本站交给下一站的标准接口。

## 7. 下一步学习建议

- **下一讲 u4-l1（黄金参考与测试向量）** 会切换到**验证**侧：本讲产出的 SV 是否正确？项目用 PyTorch 当唯一真相源生成测试向量。建议先读 `sim/gen_tb_data.py`。
- **u5-l1（Yosys + slang 前端）** 是本站的直接下游：它通过 `sources.f` 读入本站产出的这批 `.sv`（含挂接的 FP 文件），用 yosys-slang 综合成 RTLIL。重点看它如何处理这些 extern 模块。
- **u6-l3（浮点原语近似）** 会更深入地剖析 `circt_fp_primitives.sv` 的定点实现与精度代价，本讲只是入门。
- 想理解 FP extern 的**来源**补丁全貌，可浏览 [patches/circt-task3-rfp/](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/patches/circt-task3-rfp/) 目录下的文件名，归纳补丁要解决的问题类别（u6-l4 会系统讲解）。
