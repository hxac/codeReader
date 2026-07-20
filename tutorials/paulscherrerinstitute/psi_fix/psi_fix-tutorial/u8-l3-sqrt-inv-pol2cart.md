# sqrt / inv / pol2cart_approx 函数实现

## 1. 本讲目标

本讲是「函数近似与代码生成」单元的收口讲义。在前两讲（u8-l1、u8-l2）里，我们已经掌握了「一个 `psi_fix_lin_approx_calc` 内核 + N 张表 = N 个函数组件」的生成式套路，并且知道 `sin18b / sqrt18b / inv18b / gaussify20b` 四张表都由 `psi_fix_lin_approx.py` 一次性算出。

但这些被生成的 `lin_approx_*18b` 组件本身有一个硬约束：**每张表只在一段很窄的输入区间内有效**。例如 `sqrt18b` 的表只在 \([0.25, 1.0)\) 内有效，`inv18b` 的表只在 \([1.0, 2.0)\) 内有效。真实的输入信号不可能恰好落在这段窄区间里。

学完本讲，你应该能够：

1. 说清楚 `psi_fix_sqrt` 如何用「归一化 → 近似 → 反归一化」三段式，把任意正数映射进 `sqrt18b` 表的有效区间，再还原回去。
2. 说清楚 `psi_fix_inv`（\(1/x\)）与 `sqrt` 的结构几乎同构，但移位方向、移位次数、符号处理这三处关键差异。
3. 理解 `psi_fix_pol2cart_approx` 用「一次 sin/cos 双查表 + 两个乘法器」实现极坐标→直角坐标，并能与 u5-l2 的 CORDIC 旋转法在资源与精度上做取舍对比。
4. 看懂这三个组件共享的位真验证套路：Python 黄金模型 + preScript 协同仿真 + `###ERROR###` 逐位比对。

## 2. 前置知识

本讲直接承接 u8-l1 与 u8-l2，默认你已经理解：

- **分段线性近似内核**（u8-l1）：`psi_fix_lin_approx_calc` 用「表查 offset/gradient + 一次乘一次加」逼近连续函数，输入被拆成索引与余数，逼近误差随段数下降。
- **表的有效区间 `validRange`**（u8-l2）：每个 `CONFIGS` 在生成表时只在 `validRange` 内采样，超出该区间的输入没有正确的表项。四个标准配置如下（来自 [model/psi_fix_lin_approx.py:79-113](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L79-L113)）：

| 配置 | 被近似的函数 | `inFmt` | `outFmt` | `validRange` |
|------|------------|---------|----------|--------------|
| `Sin18Bit` | \(\sin(2\pi x)\) | (0,0,20) | (1,0,17) | 全圈 \([0,1)\) |
| `Sqrt18Bit` | \(\sqrt{x}\) | (0,0,20) | (0,0,17) | \([0.25,\,1)\) |
| `Invert18Bit` | \(1/x\) | (0,1,18) | (0,0,18) | \([1.0,\,2.0)\) |

- **移位即改变二进制小数点位置**（u1-l4、u2-l2）：对定点数左移 \(k\) 位等价于乘 \(2^k\)，右移 \(k\) 位等价于乘 \(2^{-k}\)。这是本讲「归一化/反归一化」的全部数学基础。

本讲要解决的核心矛盾就是：**表的 `validRange` 很窄，而输入信号范围很宽，两者怎么对接？** 答案是「动态移位 + 移位计数器」，这也是 `sqrt`/`inv` 两个组件区别于纯查表组件（如 `pol2cart_approx`）的根本特征。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/psi_fix_sqrt.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_sqrt.vhd) | 平方根组件：归一化 → `sqrt18b` 近似 → 半速反归一化 |
| [hdl/psi_fix_inv.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_inv.vhd) | 倒数 \(1/x\) 组件：取绝对值 → 归一化 → `inv18b` 近似 → 全速反归一化 → 复原符号 |
| [hdl/psi_fix_pol2cart_approx.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pol2cart_approx.vhd) | 极坐标→直角坐标近似：`sin18b_dual` 双查表 + 两个乘法器 |
| [model/psi_fix_sqrt.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_sqrt.py) | `sqrt` 的 Python 位真模型（黄金参考） |
| [model/psi_fix_inv.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_inv.py) | `inv` 的 Python 位真模型 |
| [model/psi_fix_pol2cart_approx.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pol2cart_approx.py) | `pol2cart_approx` 的 Python 位真模型 |
| [model/psi_fix_lin_approx.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py) | 近似内核 + 代码生成器（u8-l2 已详解，本讲只复用其 `CONFIGS`） |

三个组件的协同仿真测试台与 preScript 在 `testbench/psi_fix_{sqrt,inv,pol2cart_approx}_tb/` 下，结构同 u3-l2 的 mov_avg 套路，本讲在「代码实践」中会引用。

---

## 4. 核心概念与源码讲解

### 4.1 平方根实现（psi_fix_sqrt）

#### 4.1.1 概念说明

开方是一个非线性函数，定点硬件没有原生指令。本组件的策略是「**把任意正数动态移位到 `sqrt18b` 表的有效区间 \([0.25, 1)\) 内，查表开方，再把结果反向移位回去**」。

为什么是 \([0.25, 1)\) 这段？因为 \(\sqrt{x}\) 在这段的输出落在 \([0.5, 1)\)，正好填满一个无符号 \([0,1)\) 定点数的高位，精度最高；而如果直接对极小的 \(x\)（例如 \(2^{-10}\)）查表，结果 \(2^{-5}\) 会挤在低位、丢精度。

数学依据是开方对 2 的幂的可分配性。对任意正数 \(x\)，总可以找到偶数 \(s\) 使得：

\[
x = 2^{s}\cdot x',\qquad x'\in[0.25,\,1)
\]

那么：

\[
\sqrt{x}=\sqrt{2^{s}\cdot x'}=2^{s/2}\cdot\sqrt{x'}
\]

这里有两个关键点：
- **\(s\) 必须是偶数**，这样 \(s/2\) 才是整数移位，可以精确还原（否则要移半位，二进制做不到）。
- **还原时移位量减半**（\(s/2\) 而非 \(s\)），这是开方区别于普通运算的本质。

#### 4.1.2 核心流程

`sqrt` 的数据流是一条三级流水线：

```
dat_i [0,I,F]
   │
   ├─[Stage0] 粗归一化: 右移 NormSft_c 位, 把整数位清掉 → [0,0,W] 纯小数
   │
   ├─[Shift Stages] 细归一化: 二进制搜索左移 sft 位, 把值送进 [0.25,1)
   │     · 同时用 SftCnt 记录移了多少位 (优先编码器式逐级判定)
   │     · 同时检测 IsZero (输入为 0 则结果强制为 0)
   │
   ├─[sqrt18b] 分段线性近似 √x, 仅在 [0.25,1) 有效  (sub-component)
   │
   ├─[FIFO]    把 SftCnt 与 IsZero 延时若干拍, 与近似结果对齐
   │
   ├─[Out Shift Stages] 反归一化: 右移 sft/2 位 (注意减半!)
   │
   └─[Output]  粗反归一化: 左移 NormSft_c/2 位 → out_fmt_g, 末端 round/sat
```

细归一化的「二进制搜索」是这样工作的：把最大可能移位量 `MaxSft_c` 拆成若干个 2 的幂（\(2^{k}, 2^{k-1}, \dots, 2^{0}\)），每个移位级处理一个 2 的幂。如果当前值左移这一档后仍不超过表的上界（即高位为 0），就移并把对应的 `SftCnt` 位置 1；否则不移。这等价于一个流水化的优先编码器，把「需要移多少位」逐位算出来。

#### 4.1.3 源码精读

先看一组决定全部位宽的常量（[hdl/psi_fix_sqrt.vhd:42-50](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_sqrt.vhd#L42-L50)）：

```vhdl
constant InFmtNorm_c          : psi_fix_fmt_t := (0, 0, in_fmt_g.I + in_fmt_g.F);          -- 粗归一化后的纯小数格式
constant OutFmtNorm_c         : psi_fix_fmt_t := (out_fmt_g.S, 0, out_fmt_g.I + out_fmt_g.F + 1); -- 保留 1 位舍入余量
constant SqrtInFmt_c          : psi_fix_fmt_t := (0, 0, 20);   -- sqrt18b 表的输入格式 (与 CONFIGS 一致)
constant SqrtOutFmt_c         : psi_fix_fmt_t := (0, 0, 17);   -- sqrt18b 表的输出格式
constant MaxSft_c             : natural     := (InFmtNorm_c.F / 2 * 2);          -- 最大移位量(强制偶数)
constant SftStgBeforeApprox_c : natural     := log2(MaxSft_c);                   -- 归一化移位级数
constant SftStgAfterApprox_c  : natural     := SftStgBeforeApprox_c / 2;         -- 反归一化级数(减半!)
constant NormSft_c            : integer     := (in_fmt_g.I + 1) / 2 * 2;         -- 粗移位量(向上取偶)
```

注意三点：① `SftStgAfterApprox_c = SftStgBeforeApprox_c / 2`，这正是「开方还原移位减半」的物化；② `MaxSft_c` 与 `NormSft_c` 都用 `/2*2` 强制取偶，对应「\(s\) 必须为偶数」；③ `log2` 来自外部库 `psi_common_math_pkg`（下取整的以 2 为底对数），它给出覆盖最大移位所需的二进制搜索级数。

**粗归一化**（[hdl/psi_fix_sqrt.vhd:105](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_sqrt.vhd#L105)）把整数位右移清掉，得到纯小数：

```vhdl
v.Norm_0 := psi_fix_shift_right(dat_i, in_fmt_g, NormSft_c, NormSft_c, InFmtNorm_c, psi_fix_trunc, psi_fix_wrap);
```

**细归一化的移位级循环**（[hdl/psi_fix_sqrt.vhd:108-127](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_sqrt.vhd#L108-L127)）是本组件的算法核心。每一级处理 \(2^{\text{SftStgBeforeApprox\_c}-stg}\) 位的移位，判定高位是否为 0 来决定移不移：

```vhdl
for stg in 0 to SftStgBeforeApprox_c - 1 loop
  ...
  SftBefore_v := 2**(SftStgBeforeApprox_c - stg);
  if unsigned(SftBeforeIn_v(... downto ...)) = 0 then
    v.InSft(stg) := SftBeforeIn_v(... downto 0) & zeros_vector(SftBefore_v);  -- 左移, 低位补 0
    v.SftCnt(stg)(SftStgBeforeApprox_c - stg - 1) := '1';                     -- 记下: 这一位移了
  else
    v.InSft(stg) := SftBeforeIn_v;                                            -- 不移
    v.SftCnt(stg)(SftStgBeforeApprox_c - stg - 1) := '0';
  end if;
end loop;
```

这本质上是一个展开成流水线的优先编码器：`SftCnt` 的每一位对应「是否移了某一档」，所有位拼起来就是总移位量 \(s\)。

随后调用 `sqrt18b` 近似内核（[hdl/psi_fix_sqrt.vhd:183-191](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_sqrt.vhd#L183-L191)）：

```vhdl
inst_sqrt : entity work.psi_fix_lin_approx_sqrt18b
  port map(clk_i => clk_i, rst_i => rst_i,
           vld_i => r.InVld(r.InVld'high), dat_i => SqrtIn_s,
           vld_o => SqrtVld_s,  dat_o => SqrtData_s);
```

由于 `sqrt18b` 内核有若干拍流水延迟，`SftCnt` 必须被同样延时才能与结果对齐。这里用一个 `psi_common_sync_fifo` 当延时线（[hdl/psi_fix_sqrt.vhd:194-214](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_sqrt.vhd#L194-L214)），把 `IsZero` 标志与 `SftCnt` 拼一起送进去。代码注释明确写了动机：用 FIFO 而非固定打拍数，是为了「**将来近似内核延迟变了也不用改这边**」。

**反归一化移位级**（[hdl/psi_fix_sqrt.vhd:139-148](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_sqrt.vhd#L139-L148)）每个移位级的步长是 `2**(2*StgIdx_v)`——注意指数里有 `2*`，等价于「移位量翻倍、移位级数减半」，正是 \(s \to s/2\) 的硬件体现：

```vhdl
StgIdx_v       := SftStgAfterApprox_c - 1 - stg;
SftStepAfter_v := 2**(2 * (StgIdx_v));
v.OutSft(stg+1) := psi_fix_shift_right(r.OutSft(stg), OutFmtNorm_c,
                    to_integer(r.OutCnt(stg)(2*StgIdx_v+1 downto 2*StgIdx_v)) * SftStepAfter_v,
                    3 * SftStepAfter_v, OutFmtNorm_c, psi_fix_trunc, psi_fix_wrap, true);
```

最后是粗反归一化与末端量化（[hdl/psi_fix_sqrt.vhd:151](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_sqrt.vhd#L151)），左移 `NormSft_c/2`（再次减半），并在这一步才做用户指定的 `round/sat`：

```vhdl
v.OutRes := psi_fix_shift_left(r.OutSft(r.OutSft'high), OutFmtNorm_c, NormSft_c/2, NormSft_c/2, out_fmt_g, round_g, sat_g);
```

输入必须无符号，否则综合期断言报错（[hdl/psi_fix_sqrt.vhd:82](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_sqrt.vhd#L82)）：

```vhdl
assert in_fmt_g.S = 0 report "###ERROR###: psi_fix_sqrt in_fmt_g must be unsigned!" severity error;
```

#### 4.1.4 代码实践

**实践目标**：对照 Python 黄金模型，亲手验证「偶数移位 + 减半还原」的开方策略。

**操作步骤**（源码阅读型实践）：

1. 打开 [model/psi_fix_sqrt.py:25-47](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_sqrt.py#L25-L47)，找到 `Process` 方法。
2. 定位这一行，它把每样本的移位量强制取偶：
   ```python
   sft = (np.ceil(-np.log2(d_norm+1e-12)/2)-1)*2
   ```
   注意末尾的 `*2` 和外面的 `/2` —— 这保证 `sft` 永远是偶数。
3. 再看还原这两行，确认还原移位量恰好是输入移位量的一半：
   ```python
   resSft = psi_fix_shift_right(sftIn, ..., sft / 2, ...)   # sft 的一半
   denorm = psi_fix_shift_left(resSft, ..., normSft/2, ...) # 粗移位也减半
   ```
4. 现在看 preScript 给的固定刺激 [testbench/psi_fix_sqrt_tb/Scripts/preScript.py:36](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_sqrt_tb/Scripts/preScript.py#L36)：
   ```python
   stim = np.concatenate(([1/8, 1/4, 1/2, 1, 2, 4], stimSimple, stimRand))
   ```
   注意前 6 个是 \(2\) 的幂（跨 5 个二进制数量级），刻意覆盖不同移位档位。

**需要观察的现象**：对 \(x=1/8\)，模型应左移 `sft=2` 位把它从 \(0.125\) 搬到 \(0.5\in[0.25,1)\)，查表得 \(\sqrt{0.5}\approx0.707\)，再右移 `sft/2=1` 位还原成 \(\approx0.354=\sqrt{0.125}\)。

**预期结果**：手算 \(\sqrt{1/8}\approx0.3536\)，与上述还原结果一致，证明「偶数移位 + 减半还原」数学成立。完整 RTL 比对需本地跑回归（见第 5 节），若无仿真器则为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MaxSft_c` 与 `NormSft_c` 都要用 `/2*2` 强制取偶？如果允许奇数移位会怎样？

**答案**：因为开方还原时移位量要减半（\(s/2\)）。若 \(s\) 是奇数，\(s/2\) 就不是整数，二进制无法做「移半位」，结果会丢失精度或无法对齐。强制偶数保证还原移位始终是整位。

**练习 2**：`SftStgAfterApprox_c` 为什么恰好是 `SftStgBeforeApprox_c / 2`？

**答案**：归一化时把移位量 \(s\) 编码进 `SftCnt` 需要 \(\log_2(\text{MaxSft})\) 位；反归一化只需还原 \(s/2\)，其取值范围缩半，编码位数也减半，故移位级数减半。

---

### 4.2 倒数实现（psi_fix_inv）

#### 4.2.1 概念说明

`psi_fix_inv` 计算 \(1/x\)，结构与 `sqrt` 几乎同构——同样是「归一化 → 查表近似 → 反归一化」。但它有三个本质差异，全部源自 \(1/x\) 与 \(\sqrt{x}\) 的数学性质不同：

1. **表的有效区间不同**：`inv18b` 在 \([1.0, 2.0)\) 有效（见 [model/psi_fix_lin_approx.py:105-113](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L105-L113)），所以归一化目标是 \([1,2)\) 而非 \([0.25,1)\)。
2. **还原移位不减半**：因为
   \[
   \frac{1}{2^{s}\cdot x'}=2^{-s}\cdot\frac{1}{x'}
   \]
   倒数对 2 的幂的指数是「直接取反」，不是「减半」。所以反归一化的移位量与归一化相同（都是 \(s\)），移位级数也不减半。
3. **\(1/x\) 对负数有定义**：组件先取绝对值算 \(1/|x|\)，最后按原符号还原（负数的倒数仍是负数）。`sqrt` 不需要这一步。

#### 4.2.2 核心流程

```
dat_i [s,I,F]
   │
   ├─[Stage0] 取绝对值 abs(dat_i) → AbsFull, 记下符号 InSign (正数记 0)
   ├─[Stage1] resize 回原格式 (饱和, 防 abs 溢出位)
   ├─[Stage2] 粗归一化: 移到 [1,2) 区间
   ├─[Shift Stages] 细归一化: 左移 sft 位, 送进 [1,2), SftCnt 记移位量
   │
   ├─[inv18b] 分段线性近似 1/x, 仅在 [1,2) 有效  (sub-component)
   │
   ├─[FIFO]   SftCnt + 符号 延时对齐
   │
   ├─[Out Shift Stages] 反归一化: 左移 sft 位 (注意: 不减半, 而且是左移!)
   ├─[Denorm] 粗反归一化
   └─[Sign]   若原符号为负, 对结果取 neg (求负)
```

特别注意反归一化方向：`inv` 是**左移**（乘 \(2^{s}\)）。原因是归一化时把 \(x\) 左移到 \([1,2)\) 等于乘了 \(2^{s}\)，那么 \(1/x\) 就要乘 \(2^{-s}\)... 但组件里 `outFmtNorm` 的尺度安排使得这一步表现为左移（详见 4.2.3 的格式推导）。直觉上：输入越大，倒数越小；归一化把小数放大了，结果就得相应缩小——具体移位方向由 `inFmtNorm`/`outFmtNorm` 的整数位差异决定。

#### 4.2.3 源码精读

常量定义（[hdl/psi_fix_inv.vhd:35-44](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_inv.vhd#L35-L44)），与 `sqrt` 对照看差异：

```vhdl
constant InFmtNorm_c          : psi_fix_fmt_t := (0, 1, in_fmt_g.I + in_fmt_g.F);   -- 归一化目标有 1 个整数位 → [1,2)
constant OutFmtNorm_c         : psi_fix_fmt_t := (0, 1 + in_fmt_g.F, out_fmt_g.I + out_fmt_g.F);
constant InvInFmt_c           : psi_fix_fmt_t := (0, 1, 18);   -- inv18b 表输入 (与 CONFIGS 一致)
constant InvOutFmt_c          : psi_fix_fmt_t := (0, 0, 18);   -- inv18b 表输出
constant MaxSft_c             : natural     := InFmtNorm_c.F;
constant SftStgBeforeApprox_c : natural     := log2ceil(MaxSft_c);                  -- 注意: 用 log2ceil (上取整)!
constant SftStgAfterApprox_c  : natural     := SftStgBeforeApprox_c;                -- 注意: 不减半!
constant NormSft_c            : integer     := in_fmt_g.I - 1;
```

两处与 `sqrt` 的鲜明对比：
- `SftStgAfterApprox_c = SftStgBeforeApprox_c`（**不减半**），因为 \(1/x\) 还原移位不减半。
- 用 `log2ceil`（上取整）而非 `sqrt` 的 `log2`（下取整），细节差异源于两者 `MaxSft_c` 定义不同，所需二进制搜索级数不同；它只是「覆盖最大移位所需的级数」这一工程量，不影响算法思想。

符号与绝对值处理（[hdl/psi_fix_inv.vhd:106-114](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_inv.vhd#L106-L114)）：

```vhdl
if in_fmt_g.S = 0 then
  v.InSign(0) := '0';                       -- 无符号输入: 恒正
else
  v.InSign(0) := dat_i(dat_i'high);         -- 有符号输入: 记符号位
end if;
v.AbsFull_0 := psi_fix_abs(dat_i, in_fmt_g, AbsFullFmt_c, psi_fix_trunc, psi_fix_wrap);
```

粗归一化同时支持左移和右移（[hdl/psi_fix_inv.vhd:117-121](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_inv.vhd#L117-L121)），因为 `NormSft_c = in_fmt_g.I - 1` 可正可负（取决于输入整数位）：

```vhdl
if NormSft_c > 0 then
  v.Norm_2 := psi_fix_shift_right(r.Abs_1, AbsFmt_c, NormSft_c, ...);
else
  v.Norm_2 := psi_fix_shift_left(r.Abs_1, AbsFmt_c, -NormSft_c, ...);
end if;
```

反归一化移位级（[hdl/psi_fix_inv.vhd:152-162](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_inv.vhd#L152-L162)），与 `sqrt` 对照：步长是 `2**(StgIdx_v)`（**没有 `sqrt` 里的 `2*`**），且用 `psi_fix_shift_left`（左移）：

```vhdl
StgIdx_v       := SftStgAfterApprox_c - 1 - stg;
SftStepAfter_v := 2**(StgIdx_v);                                    -- sqrt 这里是 2**(2*StgIdx_v)
v.OutSft(stg+1) := psi_fix_shift_left(r.OutSft(stg), OutFmtNorm_c,
                    to_integer(r.OutCnt(stg)(StgIdx_v downto StgIdx_v)) * SftStepAfter_v,
                    SftStepAfter_v, OutFmtNorm_c, psi_fix_trunc, psi_fix_wrap, true);
```

末端复原符号（[hdl/psi_fix_inv.vhd:172-176](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_inv.vhd#L172-L176)）：

```vhdl
if in_fmt_g.S = 0 or r.OutSign(r.OutSign'high) = '0' then
  v.OutRes := r.Denorm;                                              -- 正数: 直接输出
else
  v.OutRes := psi_fix_neg(r.Denorm, out_fmt_g, out_fmt_g, psi_fix_trunc, sat_g);  -- 负数: 求负
end if;
```

实例化的近似内核是 `inv18b`（[hdl/psi_fix_inv.vhd:208-216](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_inv.vhd#L208-L216)，注意例化名仍叫 `inst_sqrt`，是复制 sqrt 改造时留下的命名痕迹，但实体已正确指向 `psi_fix_lin_approx_inv18b`）。FIFO 同样用于把 `SftCnt` 与符号延时对齐（[hdl/psi_fix_inv.vhd:219-241](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_inv.vhd#L219-L241)），额外加了 `SftCntLim` 把超过 `MaxSft_c` 的计数值钳位（防溢出）。

> **顺带提醒**：`1/0\) 无定义。preScript 在比对时用 `exp = 1/(stimQuant+1e-12)` 防除零（[testbench/psi_fix_inv_tb/Scripts/preScript.py:49](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_inv_tb/Scripts/preScript.py#L49)），实际硬件对 0 输入的行为需要在使用时自行约束。

#### 4.2.4 代码实践

**实践目标**：用一个最小 Python 脚本，亲手跑 `inv` 模型并验证「绝对值 → 归一化 → 倒数 → 还原符号」全链路。

**操作步骤**：

1. 确认 `model/` 与并排摆放的 `en_cl_fix` 在 Python 路径上（参考 u2-l3 的 `sys.path` 约定）。
2. 写一段最小调用（**示例代码**，非项目原有）：
   ```python
   import sys; sys.path.append("model")
   import numpy as np
   from psi_fix_inv import psi_fix_inv
   from psi_fix_pkg import *
   inFmt  = psi_fix_fmt_t(1, 4, 14)   # 与 preScript 一致
   outFmt = psi_fix_fmt_t(1, 1, 15)
   m = psi_fix_inv(inFmt, outFmt, psi_fix_rnd_t.round, psi_fix_sat_t.sat)
   x  = np.array([-2.0, -0.5, 0.5, 2.0])
   print(m.Process(x))                # 期望 ≈ [-0.5, -2.0, 2.0, 0.5]
   ```

**需要观察的现象**：输入正负四个数，输出符号与输入相反、幅度为输入倒数。负数 \(-2\) 给出 \(-0.5\)，证明符号复原生效。

**预期结果**：约打印 `[-0.5, -2.0, 2.0, 0.5]`（受定点量化影响有微小误差）。完整 RTL 逐位比对需本地跑 `psi_fix_inv_tb`，若无仿真器则「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：列出 `psi_fix_inv` 相对于 `psi_fix_sqrt` 的三处结构差异，并分别说出各自的数学根源。

**答案**：① 归一化目标区间是 \([1,2)\) 而非 \([0.25,1)\)——根源是 `inv18b` 表的 `validRange`；② 反归一化移位量与级数不减半——根源是 \(1/(2^s\cdot x')=2^{-s}\cdot(1/x')\)，指数直接取反；③ 多了取绝对值与末端复原符号——根源是 \(1/x\) 对负数有定义且符号不变。

**练习 2**：`SftStgAfterApprox_c` 在 `inv` 里为什么不必除以 2？

**答案**：因为倒数的还原移位是 \(s\)（与归一化相同），不是 \(s/2\)。需要编码的移位量范围没缩半，级数自然不必减半。

---

### 4.3 极坐标到直角坐标近似（psi_fix_pol2cart_approx）

#### 4.3.1 概念说明

`pol2cart_approx` 把极坐标 \((r,\theta)\)（幅度 \(r\)、相位 \(\theta\)）变换为直角坐标 \((I,Q)\)：

\[
I = r\cdot\cos(2\pi\theta),\qquad Q = r\cdot\sin(2\pi\theta)
\]

其中 \(\theta\) 用「圈的分数」表示（\([0,1)\) 对应一整圈 \(2\pi\)），这正是 `Sin18Bit` 表的输入约定（`function=lambda x: np.sin(x*2*np.pi)`，见 [model/psi_fix_lin_approx.py:79-86](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L79-L86)）。

与 `sqrt`/`inv` 不同，这里**不需要归一化/反归一化**——因为相位 \(\theta\) 本来就是 \([0,1)\) 的圈分数，正好落在 sin 表的全圈有效区间内。所以这个组件很「轻」：一次 sin/cos 双查表 + 两个乘法器。

一个巧妙的简化：**只存 sin 一张表，cos 用相位偏移 0.25 圈（即 \(90°\)）来算**：

\[
\cos(2\pi\theta)=\sin\bigl(2\pi(\theta+0.25)\bigr)
\]

于是同一个 `sin18b` 内核既出 sin 又出 cos，省掉一半表存储。库里有现成的双通道版本 `psi_fix_lin_approx_sin18b_dual`，可以同时算两路相位。

#### 4.3.2 核心流程

```
dat_abs_i (r), dat_ang_i (θ)   θ ∈ [0,1) 圈
   │
   ├─[Stage0] 寄存输入, abs 进延时线 AbsPipe (要陪 sin/cos 流水 7 拍)
   ├─[Stage1] 相位重定格式: PhaseSin = resize(θ)
   │            PhaseCos = θ + 0.25   ← 用加 0.25 圈得到 cos 相位
   ├─[Stage2..8] sin18b_dual 双查表 (sub-component), 同时出 sin(θ) 与 sin(θ+0.25)=cos(θ)
   ├─[Stage9]  两个乘法器: I = r·cos, Q = r·sin
   └─[Stage10] resize 到 out_fmt_g, 末端 round/sat
```

整条流水固定 10 拍以上，吞吐 1 样本/周期。幅度 \(r\) 在 `AbsPipe` 里逐级打拍，纯粹是为了与查表 7 拍延迟对齐，到第 9 级才参与乘法。

#### 4.3.3 源码精读

常量定义（[hdl/psi_fix_pol2cart_approx.vhd:40-43](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pol2cart_approx.vhd#L40-L43)），`CosOffs_c` 就是「0.25 圈」的定点常数：

```vhdl
constant SinOutFmt_c : psi_fix_fmt_t := (1, 0, 17);                            -- sin18b 输出格式
constant SinInFmt_c  : psi_fix_fmt_t := (0, 0, 20);                            -- sin18b 输入格式 (圈分数)
constant MultFmt_c   : psi_fix_fmt_t := (1, in_abs_fmt_g.I + SinOutFmt_c.I,
                                         in_abs_fmt_g.F + SinOutFmt_c.F);       -- r·sin 乘积的满精度格式
constant CosOffs_c   : ... := psi_fix_from_real(0.25, SinInFmt_c);             -- 0.25 圈 = 90°
```

相位处理（[hdl/psi_fix_pol2cart_approx.vhd:105-108](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pol2cart_approx.vhd#L105-L108)）：sin 相位只做 resize，cos 相位用加 0.25 实现：

```vhdl
v.PhaseSin_1 := psi_fix_resize(r.PhaseIn_0, in_angle_fmt_g, SinInFmt_c, round_g, psi_fix_wrap);
v.PhaseCos_1 := psi_fix_add(r.PhaseIn_0, in_angle_fmt_g,
                            CosOffs_c, SinInFmt_c,
                            SinInFmt_c, round_g, psi_fix_wrap);
```

双查表与乘法（[hdl/psi_fix_pol2cart_approx.vhd:115-116](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pol2cart_approx.vhd#L115-L116)）：`b` 路送进的是 cos 相位、出来的是 cos，乘幅度得 \(I\)；`a` 路 sin 乘幅度得 \(Q\)：

```vhdl
v.MultI_9 := psi_fix_mult(r.AbsPipe(8), in_abs_fmt_g, CosData_8, SinOutFmt_c, MultFmt_c, psi_fix_trunc, psi_fix_wrap);
v.MultQ_9 := psi_fix_mult(r.AbsPipe(8), in_abs_fmt_g, SinData_8, SinOutFmt_c, MultFmt_c, psi_fix_trunc, psi_fix_wrap);
```

注释特意说明 `MultFmt_c`「格式足够，不会发生舍入/截断」——即乘积用满精度格式接住，量化延迟到末端 resize（[hdl/psi_fix_pol2cart_approx.vhd:120-121](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pol2cart_approx.vhd#L120-L121)）：

```vhdl
v.OutI_10 := psi_fix_resize(r.MultI_9, MultFmt_c, out_fmt_g, round_g, sat_g);
v.OutQ_10 := psi_fix_resize(r.MultQ_9, MultFmt_c, out_fmt_g, round_g, sat_g);
```

双查表内核例化（[hdl/psi_fix_pol2cart_approx.vhd:149-165](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pol2cart_approx.vhd#L149-L165)），`a`/`b` 两路分别接 sin 与 cos 相位：

```vhdl
i_sincos : entity work.psi_fix_lin_approx_sin18b_dual
  port map(clk_i => clk_i, rst_i => rst_i,
           vld_a_i => r.VldIn(1), dat_a_i => r.PhaseSin_1,   -- sin 路
           vld_b_i => r.VldIn(1), dat_b_i => r.PhaseCos_1,   -- cos 路 (相位+0.25)
           vld_a_o => SinVld_8, dat_a_o => SinData_8,
           vld_b_o => CosVld_8, dat_b_o => CosData_8);
```

组件还有一组运行期断言（[hdl/psi_fix_pol2cart_approx.vhd:74-82](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pol2cart_approx.vhd#L74-L82)），持续监测「sin 路与 cos 路的 valid 必须同拍、且与流水 valid 对齐」，失配即打印 `###ERROR###`——这是双通道查表协同的护栏。

端口约束由综合期断言把关（[hdl/psi_fix_pol2cart_approx.vhd:70-72](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pol2cart_approx.vhd#L70-L72)）：幅度与相位都必须无符号、且相位整数位 \(I\le 0\)（即必须是 `(0,0,x)` 圈分数格式）。

#### 4.3.4 代码实践

**实践目标**：验证「加 0.25 圈 = 取 cos」的等价性，并理解 sin/cos 双查表与单一 sin 表的关系。

**操作步骤**：

1. 打开 Python 模型 [model/psi_fix_pol2cart_approx.py:63-68](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pol2cart_approx.py#L63-L68)：
   ```python
   phaseSin = psi_fix_resize(inpAngle, ..., self.SIN_IN_FMT, ...)
   phaseCos = psi_fix_add(inpAngle, ..., 0.25, self.SIN_IN_FMT, ...)   # cos 相位
   sinData = self.sineApprox.Approximate(phaseSin)
   cosData = self.sineApprox.Approximate(phaseCos)                     # 同一个 sin 表
   outI = psi_fix_mult(inpAbs, ..., cosData, ..., self.outFmt, ...)
   outQ = psi_fix_mult(inpAbs, ..., sinData, ..., self.outFmt, ...)
   ```
2. 注意 `self.sineApprox` 只实例化了一次（构造函数里 [model/psi_fix_pol2cart_approx.py:51](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pol2cart_approx.py#L51)），sin 与 cos 复用同一张表。

**需要观察的现象**：当 \(\theta=0\)（圈）时，\(I=r\cdot\cos 0=r\)、\(Q=r\cdot\sin 0=0\)；当 \(\theta=0.25\) 时，\(I=0\)、\(Q=r\)。

**预期结果**：输入 \((r=1.0,\theta=0)\) 应得 \((I\approx1.0,\,Q\approx0)\)。preScript 里 `anglesLogic = np.linspace(0,1,361)` 覆盖整圈（[testbench/psi_fix_pol2cart_approx_tb/Scripts/preScript.py:38-41](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_pol2cart_approx_tb/Scripts/preScript.py#L38-L41)），逐位比对需本地跑回归，否则「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `pol2cart_approx` 不需要 `sqrt`/`inv` 那种归一化/反归一化移位级？

**答案**：因为相位 \(\theta\) 的天然范围 \([0,1)\) 圈恰好就是 `Sin18Bit` 表的全圈有效区间，输入直接落进表内，无需动态移位。幅度 \(r\) 只是做乘法，也不受表的有效区间约束。

**练习 2**：组件只例化了一张 sin 表（`sin18b_dual`），却同时给出了 sin 和 cos，这是怎么做到的？

**答案**：利用恒等式 \(\cos(2\pi\theta)=\sin(2\pi(\theta+0.25))\)。把相位加 0.25 圈（\(90°\)）后再查同一个 sin 表，得到的就是 cos。双通道版 `sin18b_dual` 用同一张表同时算两路相位，省存储。

---

## 5. 综合实践：pol2cart_approx 与 cordic_rot 的资源/精度取舍

本讲的核心实践任务是把 `psi_fix_pol2cart_approx` 与 u5-l2 讲过的 `psi_fix_cordic_rot` 做一次正面对比——两者都解决「极坐标 \((r,\theta)\) → 直角坐标 \((I,Q)\)」这同一个问题，但实现哲学截然不同。

### 实践目标

理解「**查表 + 乘法器**」与「**迭代移位加减**」两类非线性函数实现路线的取舍，能根据应用场景（资源紧张 vs 精度优先 vs 吞吐优先）做选型。

### 操作步骤

1. **读两份实体头部说明**，对照它们的数学方法：
   - `pol2cart_approx`：[hdl/psi_fix_pol2cart_approx.vhd:16-36](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_pol2cart_approx.vhd#L16-L36)（查表 + 乘法）。
   - `cordic_rot`：[hdl/psi_fix_cordic_rot.vhd:7-14](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_cordic_rot.vhd#L7-L14)（注释明确写了「pipelined = 更多逻辑但 1 样本/周期；serial = N 周期但更少逻辑」）。

2. **填出下表**（先自己判断，再对照下方参考答案）：

| 维度 | `pol2cart_approx` | `cordic_rot` |
|------|-------------------|--------------|
| 数学方法 | ？（查表 / 迭代） | ？ |
| 核心算子 | ？（乘法器 / 移位加） | ？ |
| 增益误差 | ？（有无 \(G_N\approx1.65\)） | ？ |
| 角度有效范围 | ？（全圈 / 有限收敛锥） | ？ |
| 是否需象限折叠 | ？ | ？ |
| 流水吞吐 | ？（固定 1/周期） | ？（PIPELINED/SERIAL 两档） |
| 精度由什么决定 | ？（表位数 / 迭代次数） | ？ |
| 主要硬件成本 | ？（BRAM+DSP / LUT+FF） | ？ |

3. **场景选型**：针对下面三个需求，各选一个组件并说明理由：
   - (a) 已有 DSP 资源充足、要全圈相位、要求固定高吞吐、不想做增益补偿。
   - (b) DSP 紧张、LUT/FF 富裕、可以接受串行低吞吐。
   - (c) 相位只在一个象限内小幅变化、对绝对精度要求极高。

### 参考答案（对照表）

| 维度 | `pol2cart_approx` | `cordic_rot` |
|------|-------------------|--------------|
| 数学方法 | 分段线性查表近似 | CORDIC 迭代移位加减 |
| 核心算子 | 2 个乘法器（\(r\cdot\sin,\,r\cdot\cos\)）+ sin/cos 查表 | 移位 + 加减（旋转核本身无乘法器） |
| 增益误差 | 无（sin 表直接给出三角函数值） | 有 \(G_N\approx1.65\)，需 `gain_comp_g=true` 用 2 个乘法器补偿 |
| 角度有效范围 | 全圈 \([0,1)\)（sin 表覆盖整圈） | 有限收敛锥，需半圈/象限折叠 |
| 是否需象限折叠 | 否 | 是（角度格式强制 `(1,-2,x)`，做象限判定） |
| 流水吞吐 | 固定流水，1 样本/周期 | PIPELINED=1/周期（更多逻辑）；SERIAL≈1/N 周期（更少逻辑） |
| 精度由什么决定 | sin 表位数（18 位）与段数（2048 段） | 迭代次数 `iterations_g` |
| 主要硬件成本 | BRAM（查表）+ DSP（乘法） | LUT/FF（移位加），可选 DSP（增益补偿） |

**场景选型参考**：
- (a) 选 `pol2cart_approx`：DSP 充足、要全圈、固定吞吐、无增益误差，查表法最省心。
- (b) 选 `cordic_rot`（SERIAL 模式）：无乘法器、LUT/FF 为主、低吞吐可接受，移位加法最省 DSP。
- (c) 倾向 `cordic_rot`（大 `iterations_g`）：精度由迭代次数线性提升，而查表法精度受表位数固定限制；单象限小范围正好避开收敛锥折叠的复杂度。

**一句话总结取舍**：`pol2cart_approx` 用「BRAM + DSP」换「固定高吞吐 + 无增益误差 + 全圈」，是 DSP 富裕时的首选；`cordic_rot` 用「纯移位加 + 可选串行」换「零/少乘法器」，是 DSP 紧张时的首选。两者位真验证套路完全一致（Python 模型 + preScript + `###ERROR###`），可在 [sim/config.tcl:335-339](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L335-L339)（pol2cart）与 cordic_rot 的对应条目里看到回归注册。

---

## 6. 本讲小结

- `sqrt` 与 `inv` 都遵循「**归一化 → `lin_approx_*18b` 近似 → 反归一化**」三段式，本质是用动态移位把任意输入搬进表的窄有效区间，再搬回来。
- 开方的反归一化移位**减半**（\(s/2\)），所以 `sqrt` 的 `SftStgAfterApprox_c` 减半、移位步长翻倍；倒数的反归一化移位**不减半**（\(s\)），这是两者最根本的结构差异，源于 \(\sqrt{2^s\cdot x'}=2^{s/2}\sqrt{x'}\) 与 \(1/(2^s\cdot x')=2^{-s}/x'\) 的指数律不同。
- 移位量必须为偶数（`/2*2`），保证开方的 \(s/2\) 是整位二进制移位；移位量由一个流水化的优先编码器（多级 shift stage）逐位算出，存进 `SftCnt`。
- `SftCnt` 与符号/零标志用 `psi_common_sync_fifo` 做延时线，与近似内核的流水延迟对齐——用 FIFO 而非固定打拍，是为了将来内核延迟变了也不用改外壳。
- `inv` 比 `sqrt` 多两步：入口取绝对值、出口按原符号 `neg` 还原（因为 \(1/x\) 对负数有定义且符号不变）。
- `pol2cart_approx` 不需要归一化（相位天然落在 sin 表的全圈有效区间），结构最轻：一次 `sin18b_dual` 双查表（cos 靠相位加 0.25 圈复用 sin 表）+ 两个乘法器。
- 三者共享位真契约：VHDL 外壳与 Python 模型用同一组格式算式推导中间位宽，preScript 用 `psi_fix_get_bits_as_int` 把黄金结果写成位模式整数，测试台逐位比对、`###ERROR###` 为唯一失败判据。

## 7. 下一步学习建议

本讲结束后，「函数近似与代码生成」单元（单元 8）已全部完成。建议：

1. **横向打通非线性函数实现路线**：把本讲的「查表近似」、u5 的「CORDIC 迭代」、u4-l3 的「迭代恢复式除法」放在一起对比，总结出 psi_fix 在面对「硬件无原生指令的非线性运算」时的三类套路（查表 / 移位迭代 / 比特迭代），这是阅读 u9（DDS/调制解调/噪声）前的最好热身。
2. **进入单元 9**：`psi_fix_dds_18b` 直接复用本讲的 `sin18b` 查表做数控振荡，`psi_fix_demod_real2cplx`/`mod_cplx2real` 会用到复数乘法与相位补偿，是本讲内容的系统集成化应用。
3. **想动手的读者**：仿照 `psi_fix_lin_approx.py` 的 `CONFIGS`（[model/psi_fix_lin_approx.py:78-114](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L78-L114)），尝试为自己的函数（如 \(\tan\)、\(\log_2\)）设计一个配置，理解「改精度只动配置、手写 RTL 零修改」的生成式组件族威力——这正是 u8-l2 的代码生成器与 u10-l1「贡献新组件」的衔接点。
