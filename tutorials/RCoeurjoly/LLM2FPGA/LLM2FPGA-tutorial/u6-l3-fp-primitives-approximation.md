# 浮点原语的定点近似实现

## 1. 本讲目标

本讲聚焦于降级链里一个看似边缘、却决定「TinyStories-1M 能否综合下去」的关键拼图：浮点算子被 CIRCT 降级成 `extern` 黑盒之后，谁来给它提供可综合的 SystemVerilog 实现？

读者学完后应该能够：

- 说清浮点算子为什么会变成 `hw.module.extern`，以及 `circt_fp_primitives.sv` 在流水线里挂在哪个位置。
- 看懂 Q16.16 定点表示，并能逐行解释 `f32_to_q16_16` 中 `shift = e - 23 + 16` 的每一项含义。
- 掌握 `sat32` 饱和、定点乘除 `q_mul`/`q_div` 的再缩放，以及 `exp`/`tanh`/`rsqrt` 等超越函数的近似策略。
- 评估这种定点近似对 LLM 推理精度的潜在影响，并理解为什么在项目当前阶段这种近似是「可接受的」。

## 2. 前置知识

本讲是专家层（advanced），假定你已经读过：

- **u3-l4 HW 到 SystemVerilog 导出与 FP extern 处理**：知道了 `hw_clean_to_sv.sh` 有一条「禁止裸 extern」安全门，以及 `circt_fp_primitives.sv` 被以 `zz_circt_fp_primitives.sv` 的名字追加进 `sources.f`。
- **u3-l2 CF 到 Handshake**：知道 Handshake 弹性数据流的 `valid`/`ready` 握手接口。
- **u1-l1 / u1-l4**：知道项目「全开源」硬约束，以及当前 TinyStories-1M 超配约 141 倍的结论。

下面三个术语是本讲的基础，先做通俗解释：

- **IEEE 754 binary32（f32）**：用 32 位表示一个浮点数。1 位符号、8 位指数（偏置 127）、23 位小数尾数。数值 = \( (-1)^{\text{sign}} \times 1.\text{frac} \times 2^{E-127} \)。
- **定点数（fixed-point）**：小数点位置固定的数。本讲用的是 **Q16.16**：32 位有符号整数，其中高 16 位是整数部分、低 16 位是小数部分。它的真实数值 = `存储的有符号整数 / 2^16`。
- **饱和（saturation）**：当运算结果超出可表示范围时，不是回绕（wrap），而是「夹」到最大或最小可表示值。例如 32 位有符号饱和会把任何大于 \( 2^{31}-1 \) 的数截成 \( 2^{31}-1 \)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rtl/fp/circt_fp_primitives.sv](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv) | 本讲主角。一个 `circt_fp_fixed_pkg` 包（解码/缩放/饱和/近似函数）外加十几个与 `extern` 同名同端口的 `module`，提供可综合的浮点近似实现。 |
| [scripts/pipeline/hw_clean_to_sv.sh](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh) | 上一站的导出脚本。它的「禁止裸 extern」安全门正是本讲实现的消费方：校验 `FP_PRIMS_SV` 覆盖了所有 extern 后，把该文件以 `zz_circt_fp_primitives.sv` 追加进 `sources.f`。 |
| [nix/pipeline.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix) | `mkSvDerivation` 把 `allowHwExterns` / `fpPrimsSv` 开关翻译成 `ALLOW_HW_EXTERNS` / `FP_PRIMS_SV` 环境变量，喂给上面的脚本。 |

辅助背景（不在本讲重点展开，但有助于理解「extern 从哪儿来」）：

- `patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch`：作者改了上游 CIRCT 的 `HandshakeToHW.cpp`，让 `arith.addf/mulf/...`、`math.exp/tanh/rsqrt/...` 这些浮点算子在 Handshake→HW 这一步被降级成 `hw.module.extern`，而不是试图降到具体硬件。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：①FP extern 的产生背景；②Q16.16 解码/缩放/饱和；③近似实现的精度代价。

### 4.1 FP extern 的产生背景

#### 4.1.1 概念说明

在 u3-l3 我们看到 Handshake 弹性数据流图会被降级成具体的 `hw.module`。但 Handshake 图里如果含有浮点算子（LLM 里到处都是：matmul 后的加法、LayerNorm 里的除法、Softmax 里的 `exp`、GELU/Tanh 激活……），CIRCT 上游的 `lower-handshake-to-hw` 默认并不知道怎么把它们降到具体的硬件门电路——它没有浮点降级 pattern。

作者面临三个选项：

1. 等上游 CIRCT 把浮点降级做好——不可控、遥遥无期。
2. 调用 Xilinx 闭源浮点 IP 核（如 Floating-point Operator）——直接违反项目「全开源工具链」的核心约束（见 u1-l1）。
3. 把这些浮点算子降级成 **extern 黑盒**，由项目自己提供一份可综合的 SystemVerilog 实现。

项目选了第三条路：用补丁 `0015` 改 CIRCT，让浮点算子在 Handshake→HW 时变成 `hw.module.extern`；extern 的名字按算子和类型 **mangle（混淆命名）**，例如 `arith_addf_in_f32_f32_out_f32` 表示「`arith.addf`，两个 f32 输入，一个 f32 输出」。这样 extern 名字就唯一、自描述、可被脚本匹配。

> extern 名字的命名规则就是「方言_算子_入参类型_出参类型」。本讲后续会看到 `circt_fp_primitives.sv` 里的 `module` 名字与这些 mangle 名一一对应。

#### 4.1.2 核心流程

整个浮点 extern 从产生到被实现的流程如下：

1. CIRCT 补丁 `0015`：`lower-handshake-to-hw` 把 `arith::AddFOp` 等 18 类浮点算子注册成 `ExtModuleConversionPattern`，降级为 `hw.module.extern`，名字按算子 mangle。
2. 这些 extern 保留 Handshake 的 `valid`/`ready` 握手端口（如 `in0`、`in0_valid`、`in0_ready`、`out0`、`out0_valid`、`out0_ready`）。
3. 一路降到 hw-clean.mlir，交给 `hw_clean_to_sv.sh` 导出 SystemVerilog。
4. `hw_clean_to_sv.sh` 检测到 extern，要求 `ALLOW_HW_EXTERNS=1` 且 `FP_PRIMS_SV` 覆盖全部 extern，否则 `exit 1` 拒绝导出。
5. 校验通过后，把 `FP_PRIMS_SV`（即 `circt_fp_primitives.sv`）拷成 `zz_circt_fp_primitives.sv`，追加到 `sources.f` 末尾，让下游 Yosys 能找到这些 extern 的实现。

`zz_` 前缀的目的是让该文件在 `sources.f` 里排在最后，保证定义在读到 extern 实例时已经可见。

#### 4.1.3 源码精读

文件头部的注释直接点明了实现模型——解码到 Q16.16 定点、定点运算、再编码回 f32：

```sv
// Implementation model: decode f32/f64 bit patterns into Q16.16 fixed point,
// operate in fixed point, then encode back to f32 where required.
```

这段话是 [rtl/fp/circt_fp_primitives.sv:1-5](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L1-L5) 的核心设计声明：所有浮点算子都遵循「f32 → Q16.16 → 运算 → f32」三段式。

消费侧的安全门在 `hw_clean_to_sv.sh` 里。脚本先用 `grep` 把输入里所有 `hw.module.extern` 的名字提取、去重：

```bash
grep -oE 'hw\.module\.extern[[:space:]]+@([A-Za-z_][A-Za-z0-9_]*)' "$input" \
  | sed -E 's/.*@([A-Za-z_][A-Za-z0-9_]*).*/\1/' \
  | sort -u >"$tmp_externs" || true
```

见 [scripts/pipeline/hw_clean_to_sv.sh:22-24](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L22-L24)：把每一行 `hw.module.extern @arith_addf_...` 里的模块名抽出来。

只要 `tmp_externs` 非空，默认就拒绝导出：

```bash
if [[ "${ALLOW_HW_EXTERNS:-0}" != "1" ]]; then
  echo "[hw_clean_to_sv] ERROR: extern modules found in '$input'." >&2
  ...
  exit 1
fi
```

见 [scripts/pipeline/hw_clean_to_sv.sh:26-32](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L26-L32)。这是一个 **fail-fast** 的安全门：任何未被显式放行的 extern 都会让流水线立即停下来，而不是产出一个含黑盒的、下游 Yosys 会莫名其妙失败的网表。

即便 `ALLOW_HW_EXTERNS=1`，还必须提供 `FP_PRIMS_SV`，并且它会 **逐个 extern** 检查 `FP_PRIMS_SV` 里是否有同名 `module` 定义：

```bash
while IFS= read -r mod; do
  has_impl_cmd=(grep -nE '(^module[[:space:]]+'"${mod}"'\b|^`...)' "$FP_PRIMS_SV")
  if ! "${has_impl_cmd[@]}" >/dev/null 2>&1; then
    echo "$mod" >>"$tmp_missing"
  fi
done <"$tmp_externs"
```

见 [scripts/pipeline/hw_clean_to_sv.sh:42-57](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L42-L57)。这意味着 `circt_fp_primitives.sv` 必须实现降级链里出现的 **每一个** extern，缺一个都会被报出来。

最后，导出完成后把实现文件挂上去：

```bash
if [[ -s "$tmp_externs" ]]; then
  fp_sv="$output_dir/sv/zz_circt_fp_primitives.sv"
  cp "$FP_PRIMS_SV" "$fp_sv"
  printf '%s\n' "$fp_sv" >>"$output_dir/sources.f"
fi
```

见 [scripts/pipeline/hw_clean_to_sv.sh:78-82](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L78-L82)。`zz_` 前缀让它排在 `sources.f` 末尾。

Nix 侧，`mkSvDerivation` 用 `optionalString` 把两个开关翻译成环境变量：

```nix
${pkgs.lib.optionalString allowHwExterns ''
  export ALLOW_HW_EXTERNS=1
''}
${pkgs.lib.optionalString (fpPrimsSv != null) ''
  export FP_PRIMS_SV=${fpPrimsSv}
''}
```

见 [nix/pipeline.nix:74-84](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/pipeline.nix#L74-L84)。注意开关参与派生指纹：matmul 不开（走严格默认，无浮点 extern），TinyStories-1M 在 [nix/models.nix](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/nix/models.nix) 里 `allowHwExterns = true; inherit fpPrimsSv;`，两个配置互不污染缓存。

#### 4.1.4 代码实践

**实践目标**：理解 extern 名字到 `module` 名字的对应关系，以及安全门如何强制覆盖。

**操作步骤**：

1. 在 [patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/patches/circt-task3-rfp/0015-lower-float-ops-as-externs-in-handshake-to-hw.patch) 中找到 `ExtModuleConversionPattern<arith::AddFOp>`、`<math::ExpOp>`、`<math::TanhOp>` 等条目，数一下补丁一共把多少类浮点算子改成 extern。
2. 在 `circt_fp_primitives.sv` 里 `grep '^module '`，把所有 `module` 名字列出来。
3. 对照确认：补丁注册的算子集合（addf/subf/mulf/divf/maximumf/cmpf/sitofp/uitofp/fptosi/fptoui/truncf/exp/tanh/rsqrt/fpowi/roundeven…）是否都有同名 `module`。

**需要观察的现象**：两边一一对应；任何一边多出来的名字都意味着「要么 extern 没实现、要么实现没人用」。

**预期结果**：补丁注册的算子类型数 ≈ `circt_fp_primitives.sv` 里 `module` 的个数（`cmpf` 因有 `ogt/ugt/ult` 多种谓词会展开成多个 module）。

**说明**：本实践为源码阅读型，不运行命令，结论「待本地验证」精确数字。

#### 4.1.5 小练习与答案

**练习 1**：为什么作者选择「降级成 extern + 自己写实现」，而不是直接调 Xilinx 浮点 IP？

**参考答案**：两个原因。一是项目核心约束是「全开源工具链」，Xilinx 浮点 IP 核是闭源的，违反约束（见 u1-l1）。二是把 extern 留成黑盒、再独立提供实现，让降级链能先跑通到 SystemVerilog 这一步，不被「CIRCT 不会降浮点」卡住，工程上更可控。

**练习 2**：`zz_circt_fp_primitives.sv` 里的 `zz_` 前缀起什么作用？

**参考答案**：让该文件在 `sources.f` 中排在所有 CIRCT 导出的 `.sv` 之后。Yosys/slang 读 SV 时，extern 实例需要先看到定义（或被当作 blackbox），把实现文件排在末尾可保证「定义已可见」，减少解析顺序问题。

---

### 4.2 Q16.16 解码/缩放/饱和

#### 4.2.1 概念说明

本模块是本讲的技术核心。`circt_fp_primitives.sv` 的做法不是真浮点运算，而是 **把 f32/f64 解码到 Q16.16 定点，在定点域做运算，再编码回 f32**。

Q16.16 定点表示约定：

- 用一个 32 位有符号整数 `q` 存储一个值。
- 真实数值 \( v = q / 2^{16} \)。
- 于是高 16 位是整数部分，低 16 位是小数部分。
- 表示范围：\( [-2^{15},\ 2^{15}) \approx [-32768, 32768) \)；分辨率（最小可分辨的小数）：\( 2^{-16} \approx 1.5\times 10^{-5} \)。

这种表示有两个关键性质：

1. **加减法**直接做整数加减即可（小数点对齐）。
2. **乘法**会「双倍」小数位数：两个 Q16.16 相乘结果是 Q32.32，要右移 16 位（除以 \( 2^{16} \)）才回到 Q16.16。除法相反，要先把被除数左移 16 位。

Q16.16 在 LLM 推理里是相当受限的：范围只有 ±32768，分辨率也只有 ~1e-5。但它的好处是 **纯整数运算，可综合、面积小、没有浮点 IP 依赖**。

#### 4.2.2 核心流程

f32 → Q16.16 的解码流程：

1. 拆出 IEEE 754 三段：`sign = f[31]`、`exp = f[30:23]`、`frac = f[22:0]`。
2. 特判：全零（±0）直接返回 0；`exp == 0xff`（±Inf/NaN）饱和到最大/最小值。
3. 重建尾数：规格化数补上隐含的 1，`mant = {1'b1, frac}`（24 位，代表 \( 1.\text{frac} \times 2^{23} \)）；非规格化数 `mant = {1'b0, frac}`，指数取 -126/-1022。
4. 算出无偏指数 \( e = E - 127 \)（f32）或 \( E - 1023 \)（f64）。
5. **关键位移**：`shift = e - 23 + 16`，把 24 位整数尾数移位得到 Q16.16 编码。
6. 套上符号，最后 `sat32` 饱和到 32 位有符号范围。

数学上，这是把

\[
\text{value} = \text{mant} \times 2^{e-23}
\]

（因为 `mant` 已经放大了 \( 2^{23} \) 倍）转换成 Q16.16 编码 \( q = \text{value} \times 2^{16} \)，所以

\[
q = \text{mant} \times 2^{e-23} \times 2^{16} = \text{mant} \times 2^{e - 23 + 16} = \text{mant} \times 2^{\text{shift}}.
\]

净效果是把尾数移动 \( e - 7 \) 位。

#### 4.2.3 源码精读

`sat32` 是所有运算的最后兜底，把 64 位中间结果夹回 32 位有符号范围：

```sv
function automatic logic signed [31:0] sat32(input logic signed [63:0] x);
  begin
    sat32 = (x > 64'sh000000007fffffff) ? 32'sh7fffffff :
            (x < -64'sh0000000080000000) ? 32'sh80000000 : x[31:0];
  end
endfunction
```

见 [rtl/fp/circt_fp_primitives.sv:15-20](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L15-L20)。`0x7fffffff` 是 \( 2^{31}-1 \)、`0x80000000` 是 \( -2^{31} \)。

本讲实践的焦点函数 `f32_to_q16_16`：

```sv
sign = f[31]; exp = f[30:23]; frac = f[22:0];
...
if (exp == 8'h00) begin mant = {1'b0, frac}; e = -126; end
else begin mant = {1'b1, frac}; e = $signed({1'b0, exp}) - 127; end
shift = e - 23 + 16;
scaled = $signed({40'b0, mant});
scaled = (shift >= 0) ? (scaled <<< shift) : (scaled >>> (-shift));
if (sign) scaled = -scaled;
f32_to_q16_16 = sat32(scaled);
```

见 [rtl/fp/circt_fp_primitives.sv:22-41](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L22-L41)。

对 `shift = e - 23 + 16` 的逐项解释：

- `e`：无偏指数，即真实数值里 \( 2^{e} \) 的指数。
- `-23`：`mant = {1'b1, frac}` 是把 \( 1.\text{frac} \) 放大了 \( 2^{23} \) 倍的整数。`-23` 就是在撤销这个放大，把尾数还原成「真实数值」的尺度。
- `+16`：Q16.16 编码要求把真实数值再放大 \( 2^{16} \) 倍。
- 合起来 `shift = e - 23 + 16 = e - 7`：把 24 位整数尾数整体移位，得到 Q16.16 编码。

`>>>` 是 **算术右移**（保留符号），`<<<` 是逻辑左移；`shift` 为负时走右移分支，保证小数值（如 0.5）也能正确缩小。最后符号位单独处理、`sat32` 饱和。

> **手算验证**：`f32` 的 `1.0` 有 \( E=127, e=0 \)，`mant = 2^{23}`，`shift = -7`，`scaled = 2^{23} >>> 7 = 2^{16} = 0x10000`。Q16.16 的 `1.0` 正是 \( 2^{16} \)。✓
> `0.5` 有 \( e=-1 \)，`shift = -8`，`scaled = 2^{23} >>> 8 = 2^{15} = 0x8000`，Q16.16 的 `0.5` 正是 \( 2^{15} \)。✓

`f64_to_q16_16` 的结构完全对称，只是位宽变了（11 位指数、52 位尾数），因此 `shift = e - 52 + 16`，见 [rtl/fp/circt_fp_primitives.sv:43-62](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L43-L62)——其中 `-52` 对应 f64 的 52 位小数尾数。

反向编码 `q16_16_to_f32` 做的是「找最高有效位 → 归一化 → 重装 IEEE 754 三段」：

```sv
e = msb - 16;                       // 撤销 Q16.16 的 16 位小数缩放
...
norm = (msb >= 23) ? (mag >> (msb - 23)) : (mag << (23 - msb));
exp = e + 127; frac = norm[22:0]; q16_16_to_f32 = {sign, exp, frac};
```

见 [rtl/fp/circt_fp_primitives.sv:64-88](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L64-L88)。它还处理了上下溢出（`e > 127` 装最大有限值、`e < -126` 装成 0）。

定点乘除要「再缩放」回到 Q16.16：

```sv
function automatic logic signed [31:0] q_mul(...);
  logic signed [63:0] prod;
  begin prod = $signed(a) * $signed(b); q_mul = sat32(prod >>> 16); end
endfunction

function automatic logic signed [31:0] q_div(...);
  ...
  num = $signed(a) <<< 16; quo = num / $signed(b); q_div = sat32(quo);
  ...
endfunction
```

见 [rtl/fp/circt_fp_primitives.sv:90-103](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L90-L103)。`q_mul` 把 64 位乘积右移 16 位（因为 Q16.16 × Q16.16 = Q32.32）；`q_div` 先把被除数左移 16 位（抵消 Q16.16/Q16.16 = Q0.0 的小数位丢失）。两者都以 `sat32` 收尾。

最后看一个最简单的 extern 实现，以 `arith_addf` 为例：

```sv
module arith_addf_in_f32_f32_out_f32 (
  input logic [31:0] in0, input logic in0_valid,
  input logic [31:0] in1, input logic in1_valid, input logic out0_ready,
  output logic in0_ready, output logic in1_ready, output logic [31:0] out0, output logic out0_valid
);
  import circt_fp_fixed_pkg::*;
  logic signed [31:0] a_q, b_q;
  assign a_q = f32_to_q16_16(in0); assign b_q = f32_to_q16_16(in1);
  assign out0 = q16_16_to_f32(sat32($signed(a_q) + $signed(b_q)));
  assign out0_valid = in0_valid & in1_valid;
  assign in0_ready = out0_ready & in1_valid; assign in1_ready = out0_ready & in0_valid;
endmodule
```

见 [rtl/fp/circt_fp_primitives.sv:181-191](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L181-L191)。注意三点：

1. **模块名** = extern 的 mangle 名 `arith_addf_in_f32_f32_out_f32`，端口名与 Handshake extern 完全一致，Yosys 才能匹配。
2. **数据通路**是教科书式的三段式：`f32_to_q16_16` → 定点加 → `q16_16_to_f32`。
3. **握手逻辑**是组合的：`out0_valid` 两个输入都 valid 才有效；ready 信号按弹性数据流规则反向驱动。所有 extern module 都共用这一套握手模板，区别只在中间的定点运算。

#### 4.2.4 代码实践（本讲指定实践）

**实践目标**：亲手验证 `shift = e - 23 + 16` 的含义，确认 Q16.16 解码的正确性。

**操作步骤**：

1. 打开 [rtl/fp/circt_fp_primitives.sv:22-41](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L22-L41)，逐行读 `f32_to_q16_16`。
2. 对以下三个 f32 值手算 `shift` 与 `scaled`：
   - `2.0`（\( E=128, e=1 \)）
   - `4.0`（\( E=129, e=2 \)）
   - `0.25`（\( E=125, e=-2 \)）
3. 把算出的 Q16.16 整数除以 \( 2^{16} \)，看是否还原成原值。
4. 回答：为什么 `+16` 不可省？如果没有 `+16`，函数返回的是「真实数值」还是「Q16.16 编码」？

**需要观察的现象**：每个 `shift` 都等于 `e - 7`；右移分支（小数值）和左移分支（大数值）都能正确还原。

**预期结果**：

| f32 值 | e | shift = e−23+16 | mant (整数) | scaled = mant × 2^shift | 还原 = scaled / 2^16 |
| --- | --- | --- | --- | --- | --- |
| 2.0 | 1 | −6 | \( 2^{23} \) | \( 2^{17} \) = 0x20000 | 2.0 ✓ |
| 4.0 | 2 | −5 | \( 2^{23} \) | \( 2^{18} \) = 0x40000 | 4.0 ✓ |
| 0.25 | −2 | −9 | \( 2^{23} \) | \( 2^{14} \) = 0x4000 | 0.25 ✓ |

**讨论（精度影响）**：`+16` 不可省——它正是「Q16.16 定点编码」相对于「真实数值」的那个 \( 2^{16} \) 放大因子；没有它，函数返回的只是 `mant × 2^(e-23)`，即真实数值的整数近似（0 附近的数会全部塌成 0）。这也正是 Q16.16 对 LLM 的第一层威胁：任何绝对值小于 \( 2^{-16} \approx 1.5\times10^{-5} \) 的中间量都会被量化为 0；而 LLM 里常见的注意力分数、归一化系数很多落在这个尺度以下，会被直接抹掉。

**说明**：以上为按位手算，结论与具体硬件无关；如要在仿真器里跑，需自建 testbench（项目仓库未单独提供 fp 原语 testbench），「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`f32_to_q16_16` 里 `shift` 为什么写成 `e - 23 + 16` 而不直接写成 `e - 7`？

**参考答案**：两者数值相等，但 `e - 23 + 16` 把两件语义分开写：`-23` 撤销尾数 `mant = {1'b1, frac}` 内含的 23 位小数缩放、`+16` 套上 Q16.16 的 16 位小数缩放。这种写法让代码自文档化，也方便对照 `f64_to_q16_16` 里对应的 `-52 + 16`。

**练习 2**：`q_mul` 为什么要在乘积后 `>>> 16`，而 `q_div` 要在被除数上 `<<< 16`？

**参考答案**：两个 Q16.16 相乘，小数位数翻倍变成 Q32.32，需右移 16 位（除以 \( 2^{16} \)）回到 Q16.16；两数相除会丢掉 16 位小数（Q16.16 / Q16.16 = Q0.0），所以先左移被除数 16 位补回，再做整数除法。

**练习 3**：`arith_addf` 模块的 `out0_valid = in0_valid & in1_valid`，这一行体现的是 Handshake 的什么性质？

**参考答案**：弹性数据流的「双输入节点只有在两个输入都 valid 时才能产生输出」性质。`valid`/`ready` 握手保证数据自流、可独立反压；这也是这些 extern 能无缝嵌入 CIRCT 生成的数据流图的原因。

---

### 4.3 近似实现的精度代价

#### 4.3.1 概念说明

定点解码解决了「能不能算」，本模块讨论「算得多准」。答案很直接：**很不准，但对当前项目阶段够用**。

LLM 里有大量超越函数：Softmax 里的 `exp`、GELU/Tanh 激活、LayerNorm 里的 `rsqrt`。`circt_fp_primitives.sv` 给它们各自写了一个定点多项式/迭代近似。这些近似在数值上相当粗糙：

- **exp**：4 项泰勒展开 \( 1 + x + x^2/2 + x^3/6 + x^4/24 \)，且只在 \( |x| < 8 \) 内有效。
- **tanh**：一个有理近似（类 Padé），\( |x| \geq 4 \) 直接饱和到 ±1。
- **rsqrt**：3 次牛顿迭代。

#### 4.3.2 核心流程

`q_exp_approx` 用定点泰勒级数：

\[
\exp(x) \approx 1 + x + \frac{x^2}{2} + \frac{x^3}{6} + \frac{x^4}{24},\quad |x| < 8.
\]

每一项用 `q_mul` 累乘 `x` 得到 \( x^k \)，再除以 \( k! \)（2, 6, 24）累加；超出 \( |x| \geq 8 \) 直接饱和。

`q_tanh_approx` 用有理近似：

\[
\tanh(x) \approx \frac{x(27 + x^2)}{27 + 9x^2}.
\]

`q_rsqrt_approx` 用牛顿迭代解 \( y = 1/\sqrt{x} \)：

\[
y_{n+1} = y_n \cdot \left(\frac{3}{2} - \frac{x \cdot y_n^2}{2}\right).
\]

迭代 3 次。注意 `Q3_2 = 3 <<< 15`，即定点表示的 \( 3/2 \)（`<<< 15` 而不是 `16`，等价于先 `3 << 16` 再 `/2`）。

#### 4.3.3 源码精读

`q_exp_approx`：

```sv
if (x <= -Q8) q_exp_approx = 32'sd0;
else if (x >= Q8) q_exp_approx = 32'sh7fffffff;
else begin
  sum = Q_ONE; term = Q_ONE;
  term = q_mul(term, x); acc = $signed(sum) + $signed(term);        sum = sat32(acc);
  term = q_mul(term, x); acc = $signed(sum) + ($signed(term) / 2);  sum = sat32(acc);
  term = q_mul(term, x); acc = $signed(sum) + ($signed(term) / 6);  sum = sat32(acc);
  term = q_mul(term, x); acc = $signed(sum) + ($signed(term) / 24); sum = sat32(acc);
  q_exp_approx = sum;
end
```

见 [rtl/fp/circt_fp_primitives.sv:105-119](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L105-L119)。`Q8 = 8 <<< 16` 是定点 8.0，`Q_ONE = 1 <<< 16` 是定点 1.0。`term` 反复乘 `x` 得 \( x, x^2, x^3, x^4 \)，分别除以 1/2/6/24 累加。每步 `sat32` 防止定点累加溢出。

`q_tanh_approx`：

```sv
if (x >= Q4) q_tanh_approx = Q_ONE;
else if (x <= -Q4) q_tanh_approx = -Q_ONE;
else begin
  x2 = q_mul(x, x);
  tmp = $signed(Q27) + $signed(x2); num = q_mul(x, sat32(tmp));
  tmp = $signed(Q27) + $signed(q_mul(Q9, x2)); den = sat32(tmp);
  q_tanh_approx = q_div(num, den);
end
```

见 [rtl/fp/circt_fp_primitives.sv:121-133](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L121-L133)。`Q27 = 27 <<< 16`、`Q9 = 9 <<< 16`，对应公式里的常数 27 与 9。`|x| ≥ 4` 直接饱和到 ±1。

`q_rsqrt_approx`：

```sv
y = Q_ONE;
for (iter = 0; iter < 3; iter = iter + 1) begin
  y2 = q_mul(y, y); xy2 = q_mul(x, y2);
  term = sat32($signed(Q3_2) - ($signed(xy2) >>> 1));
  y = q_mul(y, term);
end
```

见 [rtl/fp/circt_fp_primitives.sv:135-149](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L135-L149)。初值取 `y = 1`（Q16.16 的 1.0），3 次迭代收敛。

#### 4.3.4 代码实践

**实践目标**：评估近似函数的有效区间与误差量级。

**操作步骤**：

1. 读 [rtl/fp/circt_fp_primitives.sv:105-149](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L105-L149) 三个近似函数。
2. 列表写出每个函数的「有效输入区间」与「区间外行为」。
3. 对照 Softmax（`exp`）、GELU（`tanh`）、LayerNorm（`rsqrt`）在 LLM 里的典型输入范围，判断哪些场景下这些近似会退化（饱和或截断）。

**需要观察的现象**：三个近似都只在很窄的区间内有效；区间外直接饱和。

**预期结果**：

| 函数 | 有效输入区间（Q16.16 真实值） | 区间外行为 |
| --- | --- | --- |
| `q_exp_approx` | \( x \in (-8, 8) \) | \( \leq -8 \) 返回 0；\( \geq 8 \) 饱和到最大 |
| `q_tanh_approx` | \( x \in (-4, 4) \) | \( \geq 4 \) 返回 1；\( \leq -4 \) 返回 −1 |
| `q_rsqrt_approx` | \( x > 0 \) | \( x \leq 0 \) 返回 0 |

**讨论**：Softmax 的 `exp` 输入是「logit 减去 max logit」，理论上 \( \leq 0 \)，但只要某次 logit 差大于 8 就会被饱和——对长序列、大词表的注意力，logit 动态范围远超 8，Softmax 分布会严重失真。LayerNorm 的 `rsqrt(方差)` 在方差很小时输入会很大，但这里反而落在有效区间。结论：**这些近似足以让降级链综合通过，但远不足以保证 LLM 数值正确**。

**说明**：源码阅读型实践；精确误差曲线「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`q_exp_approx` 用的是 4 项泰勒级数。泰勒展开在 \( |x| \) 较大时误差会怎样？代码怎么处理？

**参考答案**：泰勒级数只在展开点（这里即 0）附近收敛快，\( |x| \) 越大误差越大、甚至发散。代码用 `if (x >= Q8)` / `(x <= -Q8)` 把 \( |x| \geq 8 \) 直接饱和到最大值或 0，避免大输入下发散。

**练习 2**：为什么说这些近似「在当前项目阶段可接受」？

**参考答案**：项目当前阶段（Task 3）的目标是 **证明降级链能跑通到 SystemVerilog 并综合**，不是产出数值正确的 LLM 输出。而 TinyStories-1M 已经超配约 141 倍（见 u1-l4），根本无法在目标 FPGA 上真正推理，所以浮点精度在现阶段是「次要矛盾」。等 Task 6 把资源压下去、能上板之后，再换成真浮点或更高精度定点才有意义。

---

## 5. 综合实践

**综合任务**：为 `circt_fp_primitives.sv` 里没有的「`arith_addf` 在 f64 上的版本」（假设 CIRCT 某次降级产出了一个 `arith_addf_in_f64_f64_out_f64` extern）写一份与现有风格一致的近似实现，并解释它会被安全门怎么处理。

要求：

1. 模块名取 `arith_addf_in_f64_f64_out_f64`，端口与 [rtl/fp/circt_fp_primitives.sv:181-191](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/rtl/fp/circt_fp_primitives.sv#L181-L191) 的 `arith_addf_in_f32_f32_out_f32` 同构，但输入改成 `input logic [63:0] in0/in1`。
2. 数据通路：`f64_to_q16_16(in0) + f64_to_q16_16(in1)` → `sat32` → `q16_16_to_f32`（注意：f64 加完仍编码回 f32，因为该文件所有 extern 的「出参」都是 f32）。
3. 握手逻辑与 `arith_addf` 完全相同（`out0_valid = in0_valid & in1_valid`，ready 反向）。

**参考实现（示例代码，非项目原有）**：

```sv
module arith_addf_in_f64_f64_out_f64 (
  input logic [63:0] in0, input logic in0_valid,
  input logic [63:0] in1, input logic in1_valid, input logic out0_ready,
  output logic in0_ready, output logic in1_ready, output logic [31:0] out0, output logic out0_valid
);
  import circt_fp_fixed_pkg::*;
  logic signed [31:0] a_q, b_q;
  assign a_q = f64_to_q16_16(in0); assign b_q = f64_to_q16_16(in1);
  assign out0 = q16_16_to_f32(sat32($signed(a_q) + $signed(b_q)));
  assign out0_valid = in0_valid & in1_valid;
  assign in0_ready = out0_ready & in1_valid; assign in1_ready = out0_ready & in0_valid;
endmodule
```

**安全门行为解释**：

- 如果降级链真的产出了 `arith_addf_in_f64_f64_out_f64` 这个 extern，但 `circt_fp_primitives.sv` 里没有同名 module，`hw_clean_to_sv.sh` 的逐个校验循环（[scripts/pipeline/hw_clean_to_sv.sh:42-57](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/scripts/pipeline/hw_clean_to_sv.sh#L42-L57)）会把它写进 `tmp_missing`，最终报 `[hw_clean_to_sv] ERROR: FP_PRIMS_SV does not define all extern modules.` 并 `exit 1`。
- 把上面的 module 加进 `circt_fp_primitives.sv` 后，校验通过，它会被拷进 `zz_circt_fp_primitives.sv` 参与综合。

**精度讨论**：f64 输入被解码到 Q16.16 后，f64 原本 52 位尾数的精度被压成 16 位小数，等于主动丢掉了约 36 位精度；再加上输出又截回 f32，这条链路对「需要 f64 高精度」的场景毫无意义——这恰好印证了本讲的核心结论：**这里的定点近似是为了可综合性牺牲精度的工程妥协**。

## 6. 本讲小结

- CIRCT 补丁 `0015` 把 18 类浮点算子在 Handshake→HW 这步降级成 `hw.module.extern`，名字按算子与类型 mangle（如 `arith_addf_in_f32_f32_out_f32`）；项目用 `circt_fp_primitives.sv` 给这些 extern 提供可综合实现，从而避开「CIRCT 不会降浮点」和「闭源 IP 违反全开源约束」两个坑。
- `hw_clean_to_sv.sh` 有一道 fail-fast 安全门：默认拒绝裸 extern，只有 `ALLOW_HW_EXTERNS=1` 且 `FP_PRIMS_SV` 逐个覆盖了所有 extern 才放行，并把实现拷成 `zz_circt_fp_primitives.sv` 追加到 `sources.f`。
- 实现模型是「f32/f64 → Q16.16 定点 → 运算 → f32」三段式。`f32_to_q16_16` 里 `shift = e - 23 + 16`：`-23` 撤销尾数的 23 位小数缩放、`+16` 套上 Q16.16 的 16 位小数缩放，净效果是把 24 位整数尾数移位 `e - 7` 得到定点编码。
- `sat32` 把 64 位中间结果饱和到 32 位有符号范围；`q_mul`/`q_div` 用右移/左移 16 位做定点再缩放。
- 超越函数（`exp`/`tanh`/`rsqrt`）用定点泰勒/有理/牛顿近似，有效区间很窄（如 exp 仅 \( |x| < 8 \)），区间外直接饱和。
- **精度代价**：Q16.16 范围只有 ±32768、分辨率 ~\( 1.5\times10^{-5} \)，再加粗糙的近似函数，远不足以保证 LLM 数值正确；但这在当前阶段（证明可降级、可综合，且已超配 141 倍）是可接受的工程妥协，真要谈精度得等 Task 6 资源最小化之后。

## 7. 下一步学习建议

- **紧接着读** [patches/circt-task3-rfp/](https://github.com/RCoeurjoly/LLM2FPGA/blob/b6dc8abcc023e241016a1fe19564b0d5af0c25b4/patches/circt-task3-rfp/) 这一整个补丁栈——下一讲 u6-l4 会把它们和「141 倍超配 + nextpnr OOM」的瓶颈结论串起来，本讲的 `0015` 浮点 extern 补丁只是其中一员。
- **回顾 u3-l4**：把 `hw_clean_to_sv.sh` 的安全门与本讲的 extern 实现挂接关系对照一遍，确认你理解「检测 → 校验覆盖 → 拷贝追加」三步在脚本里的位置。
- **延伸阅读**：若要理解「为什么定点近似对 LLM 杀伤力这么大」，可对照任意一篇 Transformer 量化论文（如 LLM.int8()、GPTQ）对激活值动态范围的统计——你会发现 ±32768 / \( 2^{-16} \) 的 Q16.16 容量远小于真实激活所需。
- **后续方向**：等读到 u7-l1（注册新模型）和 u7-l3（资源优化路线）时，回过头看本讲的浮点近似——它是 Task 6「量化/换方言」要重点替换的部件之一。
