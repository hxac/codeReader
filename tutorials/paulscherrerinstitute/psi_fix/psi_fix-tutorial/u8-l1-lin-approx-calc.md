# 线性近似 lin_approx_calc 原理

## 1. 本讲目标

FPGA 上做信号处理时，常常需要计算一些「非线性函数」——正弦、平方根、倒数、高斯化（gaussify）等。这些函数没有简单的组合逻辑表达式，又不能像软件那样调用 `math.sin()`。psi_fix 用「**分段线性近似 + 查表**」给出了一套通用、可综合、且**位真**的解法。

本讲只聚焦这套解法里那个唯一的「计算内核」——`psi_fix_lin_approx_calc`。读完本讲，你应当能够：

1. 说清**分段线性近似**的数学原理，以及它为什么能用「一张表 + 一次乘法 + 一次加法」逼近任意连续函数。
2. 看懂组件如何把一个输入样本**拆成「表索引」和「段内余数」**，并理解它与外部查表实体之间的 `addr_table_o` / `data_table_i` 接口契约。
3. 沿着 7 级流水线走完一遍 `offset + gradient × reminder` 的计算过程，并能解释 VHDL 与 Python 两侧为何能逐位一致。

本讲是单元 8（函数近似与代码生成）的第一讲，只讲「计算内核」本身；如何用 Python 代码生成器自动产出那张表、并由此派生出 sin18b / sqrt18b / inv18b 等一整个组件族，留到 u8-l2。

## 2. 前置知识

本讲假设你已掌握（否则请先读对应讲义）：

- **定点格式三元组 `[s,i,f]`** 与位增长规则（u1-l4、u2-l2）。例如两个有符号数相乘 `[1,a,b]×[1,c,d] = [1,a+c+1,b+d]`，加法整数位 +1。
- **psi_fix_pkg 运算函数** `psi_fix_resize / psi_fix_mult / psi_fix_add`，及其「调用者指定结果格式、函数不自动位增长」的约定（u2-l2）。
- **两段式编码 (two-process)** 与 record 流水封装：组合进程 `p_comb` 写 `r_next`、时序进程 `p_seq` 仅打拍，`v := r` 让未赋值字段默认保持，valid 用 `std_logic_vector` 切片逐级平移（u3-l3）。
- **位真双模型**与协同仿真：VHDL 实现必须与 Python 黄金模型逐位一致，测试台用 `###ERROR###` 判失败（u3-l1、u3-l2）。
- **param_ram / ROM 查表**的基本概念（u4-l2 有过双口 RAM，本讲用到的是单口注册读 ROM）。

补充一个本讲要用到的数学常识：**线性插值**。已知函数 \(f\) 在两个邻近点 \(x_0\)、\(x_1\) 的取值，要估算中间某点 \(x\) 的函数值，最简单的办法是用一条直线去连：

\[
f(x) \approx f(x_0) + \frac{f(x_1)-f(x_0)}{x_1-x_0}\,(x-x_0)
\]

记斜率 \(g = \dfrac{f(x_1)-f(x_0)}{x_1-x_0}\)（即梯度/gradient），偏移 \(o = f(x_0)\)（即截距/offset），则：

\[
f(x) \approx o + g \cdot (x - x_0)
\]

这就是本讲全部电路要算的式子：**一次乘法 + 一次加法**。下面会看到，psi_fix 把这张「每个区间一对 (offset, gradient)」的表存进 ROM，运行时只做这一乘一加。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [hdl/psi_fix_lin_approx_calc.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd) | **本讲主角**：分段线性近似的「计算内核」。不含表，只负责拆索引/余数、做乘加。 |
| [model/psi_fix_lin_approx.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py) | Python **位真模型 + 代码生成器**。`Approximate()` 是黄金参考；`GenerateEntity()` 生成 VHDL。 |
| [model/snippets/psi_fix_lin_approx_tmpl.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lin_approx_tmpl.vhd) | 代码生成模板：例化 `lin_approx_calc` 内核 + 一张注册读 ROM，拼成具体函数（如 sin18b）。 |
| [hdl/psi_fix_lin_approx_sin18b.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_sin18b.vhd) | 由模板**生成**的 sin18b 组件（本讲用作具体例子，读它前 40 行即可）。 |
| [model/snippets/psi_fix_lin_approx_tb_tmpl.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lin_approx_tb_tmpl.vhd) | 测试台模板：`ApplyTextfileContent` 喂激励、`CheckTextfileContent` 比对响应。 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) | 回归脚本：声明 lin_approx 系列源码、测试台与运行参数。 |

**核心设计思想先记一句话**：`psi_fix_lin_approx_calc` 是「**与函数无关**」的纯算术内核——它只认 `in_fmt_g / out_fmt_g / offs_fmt_g / grad_fmt_g / table_size_g` 这几个 generic，不知道自己在算 sin 还是 sqrt。具体的函数「形状」全部编码在那张外部表里（`data_table_i`）。因此**一个内核 + N 张表 = N 个不同的函数近似组件**。这正是 u8-l2 要展开的「代码生成」叙事，本讲先把内核吃透。

## 4. 核心概念与源码讲解

### 4.1 分段线性近似原理

#### 4.1.1 概念说明

把输入区间 \([x_{\min}, x_{\max}]\) 等分成 \(P\) 段（\(P\) 即 `table_size_g`，表中点数），每段宽度：

\[
\Delta = \frac{x_{\max}-x_{\min}}{P}
\]

在第 \(k\) 段（\(k=0,\dots,P-1\)）内，用一条直线近似 \(f\)。直线由两个参数决定：

- **offset** \(o_k = f(c_k)\)：在第 \(k\) 段**中心** \(c_k = x_{\min} + (k+\tfrac{1}{2})\Delta\) 处的函数值；
- **gradient** \(g_k = f'(c_k)\)：在中心处的导数（斜率）。

于是落在第 \(k\) 段内的任意输入 \(x\)，其近似值为：

\[
\hat f(x) = o_k + g_k \cdot (x - c_k)
\]

注意「偏移量」是相对**段中心** \(c_k\) 来量的，而不是相对段左端点。这个选择不是随意的——它让段内误差关于中心对称，**最坏情况误差最小**（道理见 4.1.2）。

把 \(P\) 对 \((o_k, g_k)\) 存进一张 ROM，运行时只需：① 算出 \(x\) 落在第几段（**索引**）；② 算出 \(x\) 离段中心多远（**余数**）；③ 查表得到 \((o_k, g_k)\)；④ 做 \(o_k + g_k \cdot \text{reminder}\)。全程一次查表 + 一次乘法 + 一次加法。

#### 4.1.2 核心流程

近似误差来自「用直线代替曲线」。对二阶导数有界的函数，分段线性插值在第 \(k\) 段内的误差上界为：

\[
|\hat f(x) - f(x)| \le \frac{1}{8}\,\max_{\text{段内}}|f''|\cdot \Delta^2
\]

两个直接推论，是设计这张表时的核心权衡：

1. **误差随段宽 \(\Delta\) 平方下降**：点数 \(P\) 翻倍，误差降到 1/4。这是「加表深换精度」的杠杆。
2. **斜率越陡的区间越吃亏**：\(|f''|\) 大的地方（如 sqrt 在 0 附近、1/x 在低端）需要更密的点。所以你会看到不同 CONFIGS 的 `points` 差异很大（4.1.3）。

设计流程（已由 Python 模型自动化，见 [model/psi_fix_lin_approx.py:171-192](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L171-L192)）：

```text
1. 在 [xmin, xmax] 上等分 P 段，算出每段中心 c_k
2. offsets[k]  = f(c_k)            # scipy 直接求值
3. gradients[k] = f'(c_k)          # scipy.misc.derivative 数值求导
4. 把 offsets/gradients 量化到定点格式 offsFmt/gradFmt，落盘成表
```

#### 4.1.3 源码精读

psi_fix 在 Python 模型里把 4 个常用函数预先配成了「标准配置」`CONFIGS`，每个配置就是一组 `(function, inFmt, outFmt, offsFmt, gradFmt, points, name)`。这张表也是「为什么内核能函数无关」的最好证据——内核只吃格式，不吃函数：

[model/psi_fix_lin_approx.py:78-114](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L78-L114) —— 定义了 `Sin18Bit / Sqrt18Bit / Gaussify20Bit / Invert18Bit` 四个标准配置。注意 `points` 因函数「弯曲程度」而异：sin 用 2048 点，sqrt 只用 512 点但限定有效区间 `[0.25, ...)`（避开 0 附近 \(|f''|\to\infty\) 的奇点），1/x 用 1024 点。`name` 字段（如 `"sin18b"`）就是生成出来的实体后缀 `psi_fix_lin_approx_sin18b`。

表（offset/gradient）的实际计算与量化：

[model/psi_fix_lin_approx.py:171-192](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L171-L192) —— 用 `scipy.misc.derivative` 求每个中心的导数（行 178），用函数本身求 offset（行 179），最后 `psi_fix_from_real` 量化到 `gradFmt` / `offsFmt`（行 191-192）。`err_sat=False` 表示超界时饱和而非报错——因为设计阶段故意让格式略宽，这里只做截断式量化。

#### 4.1.4 代码实践

**实践目标**：建立「点数 ↔ 误差」的直觉，验证 4.1.2 的平方律。

**操作步骤**（源码阅读型，可运行部分标注「待本地验证」）：

1. 打开 [model/psi_fix_lin_approx.py:79-95](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L79-L95)，对比 `Sin18Bit`（points=2048）与 `Sqrt18Bit`（points=512）。
2. 阅读 `Analyze` 方法 [model/psi_fix_lin_approx.py:218-249](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L218-L249)：它会在 `validRange` 上密集采样，打印「最大误差 =多少 LSB」并画图。
3. 若本地已按 u1-l1 摆好 `en_cl_fix` 并装好 SciPy，可取消文件末尾 [model/psi_fix_lin_approx.py:355-359](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L355-L359) 的注释、改 `Design(...)` 的配置后运行，观察打印的最大误差（单位 LSB）。**待本地验证**。

**需要观察的现象**：把 `Sin18Bit` 的 `points` 从 2048 改成 1024，最大误差应大约变为原来的 4 倍（\(\Delta\) 翻倍 → \(\Delta^2\) 变 4 倍）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 offset 表存的是「段中心」的函数值 \(f(c_k)\)，而不是段左端点 \(f(x_k)\)？

> **答**：以段中心为参考点，段内偏差 \(|x-c_k| \le \Delta/2\) 关于中心对称，线性近似的截断误差上界 \(\frac{1}{8}|f''|\Delta^2\) 正是在「中心对齐」时取得的；若用左端点，最远点偏离 \(\Delta\)，误差界会变差到 \(\frac{1}{2}|f''|\Delta^2\)。

**练习 2**：sin 配置里函数写成 `np.sin(x*2*np.pi)*(1-1/2**17)`，末尾那个 `(1-1/2**17)` 因子的作用是什么？

> **答**：outFmt 是 `(1,0,17)`，有符号 18 位能表示 \((-1, +1)\) 但**取不到 +1.0**。sin 在 \(x=1/4\) 处达 +1.0，会触发饱和。乘一个略小于 1 的系数把峰值压到 +1 以下，避免饱和失真（注释 `#Prevent +1 from occurring` 说明了同一件事）。

---

### 4.2 索引 / 余数分解与表接口

#### 4.2.1 概念说明

知道了「每段存一对 (offset, gradient)」，运行时第一件事就是把输入 \(x\) 拆成两部分：

- **索引 (index)**：\(x\) 落在第几段？——决定**查表的地址**。
- **余数 (reminder)**：\(x\) 在段内、相对段中心的偏移？——作为乘法的另一个操作数。

由于输入本身就是定点数（一串二进制位），这个拆分**根本不用除法**：只要把输入的若干**高位**截下来当索引、剩下的**低位**当余数即可。具体地，若表有 \(P = 2^B\) 个点（psi_fix 的所有配置都满足 \(P\) 是 2 的幂），则需要 \(B = \lceil\log_2 P\rceil\) 个地址位：

- 输入的**最高 \(B\) 位** → 索引（表地址）；
- 输入的**其余低位** → 余数。

这正是 [hdl/psi_fix_lin_approx_calc.vhd:46-50](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L46-L50) 里那几个常量在做的事（下节精读）。

**表接口契约**：内核**不含表**，而是对外暴露一对信号，让外部（生成的包装实体）提供表内容：

- `addr_table_o`：内核**输出**的表地址（要查第几项）。
- `data_table_i`：外部**回送**的表项内容（一个拼接了 gradient 与 offset 的定宽总线）。

这条契约写死在端口声明里：[hdl/psi_fix_lin_approx_calc.vhd:37-39](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L37-L39)。

#### 4.2.2 核心流程

设输入格式 `in_fmt_g = (S, I, F)`，总位宽 \(W = S+I+F\)，表点数 \(P\)，地址位数 \(B=\lceil\log_2 P\rceil\)。则：

```text
IndexBits_c   = B                                  # 地址位宽
OffsetBits_c  = W - B                              # 余数位宽（输入去掉高 B 位剩多少）
RemFmt_c      = (0, OffsetBits_c - F, F)           # 余数格式：无符号，值域 [0, Δ)
IdxFmt_c      = (0, S+I, F - RemFmt_c.F - RemFmt_c.I)  # 索引格式：恰好 B 位
```

注意两点：

1. **余数是无符号的** `RemFmt_c.S = 0`，值域正好覆盖一段 \([0, \Delta)\)（验证见 4.2.4）。
2. **索引格式 `IdxFmt_c` 的总位宽恰好等于 \(B\)**：\(0 + (S+I) + (F - \text{RemFmt.F} - \text{RemFmt.I}) = W - \text{OffsetBits}_c = B\)。所以从输入 resize 到 `IdxFmt_c`，等价于「取输入的最高 \(B\) 位」。

> 对**有符号输入**（如 gaussify20b，`inFmt=(1,0,19)`）：索引被 resize 成**无符号**格式，于是二进制补码的负数自然映射到地址的高半区；Python 侧在造表时把负数区的那一半表项**预先轮转到表的后半段**（[model/psi_fix_lin_approx.py:176-177](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L176-L177)），两侧地址语义对齐。这是位真一致的隐藏细节。

**表数据总线的拼装顺序**（关键，VHDL 与生成器必须对齐）：

```text
data_table_i  =  [   gradient (高位)   |   offset (低位)   ]
                  <---- psi_fix_size(grad_fmt) ----><---- psi_fix_size(offs_fmt) ---->
```

即 gradient 在高位、offset 在低位。这与生成器 `gradStr & offsStr` 的拼接顺序一致（[model/psi_fix_lin_approx.py:290](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L290)）。

#### 4.2.3 源码精读

**端口的表接口**（注意方向：地址是 out、数据是 in）：

[hdl/psi_fix_lin_approx_calc.vhd:38-39](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L38-L39) —— `addr_table_o` 宽 `log2ceil(table_size_g)` 位；`data_table_i` 宽 `psi_fix_size(offs_fmt_g)+psi_fix_size(grad_fmt_g)` 位。

**用位宽算式定义拆分格式**（这是「函数无关」的精髓——全部由 generic 推导，零硬编码）：

[hdl/psi_fix_lin_approx_calc.vhd:46-52](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L46-L52)

```vhdl
constant IndexBits_c    : integer := log2ceil(table_size_g);
constant OffsetBits_c   : integer := psi_fix_size(in_fmt_g) - IndexBits_c;
constant RemFmt_c       : psi_fix_fmt_t := (0, OffsetBits_c - in_fmt_g.F, in_fmt_g.F);
constant RemFmtSigned_c : psi_fix_fmt_t := (1, RemFmt_c.I - 1, RemFmt_c.F);
constant IdxFmt_c       : psi_fix_fmt_t := (0, in_fmt_g.S + in_fmt_g.I,
                                            in_fmt_g.F - RemFmt_c.F - RemFmt_c.I);
```

其中 `RemFmtSigned_c` 是把余数「**按有符号重新解释**」的格式——它和 `RemFmt_c` 总位宽相同、小数位相同，只把最高位当作符号位（整数位 -1）。这就是 4.3 要讲的「段中心对齐」所需的那个有符号余数。

**从总线切出 offset / gradient 的子范围**（与生成器拼接顺序严格对应）：

[hdl/psi_fix_lin_approx_calc.vhd:54-55](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L54-L55) —— `OffsRng_c` 占低位、`GradRng_c` 占其上方连续位段。流水线第 3 级据此切片（见 4.3.3 的 Stage 3）。

**Python 侧用完全相同的算式**推导同一组格式，这是位真契约的「印章」：

[model/psi_fix_lin_approx.py:162-170](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L162-L170)

```python
self.indexBits = np.log2(cfg.points)
offsBits = psi_fix_size(self.cfg.inFmt) - self.indexBits
self.remFmt  = psi_fix_fmt_t(0, offsBits - self.cfg.inFmt.f, self.cfg.inFmt.f)
self.idxFmt  = psi_fix_fmt_t(0, self.cfg.inFmt.s + self.cfg.inFmt.i,
                             self.cfg.inFmt.f - self.remFmt.f - self.remFmt.i)
self.intFmt  = psi_fix_fmt_t(1, self.remFmt.i + self.cfg.gradFmt.i + 1,
                             self.remFmt.f + self.cfg.gradFmt.f)
self.addFmt  = psi_fix_fmt_t(max(self.intFmt.s, self.cfg.offsFmt.s),
                             max(self.intFmt.i, self.cfg.offsFmt.i)+1,
                             max(self.intFmt.f, self.cfg.offsFmt.f))
```

逐行对照 VHDL 的 `RemFmt_c / IdxFmt_c / IntFmt_c / AddFmt_c`——一模一样。**两侧用同一套公式推导中间格式**，是「实现各异、模型唯一」的前提（这个结论在 u7-l1 的 FIR 家族里见过，这里再次出现）。

#### 4.2.4 代码实践

**实践目标**：亲手拆一次索引/余数，验证「余数值域恰好覆盖一段」与「索引位宽恰好等于地址位宽」。

**以 sin18b 为例**（`inFmt=(0,0,20)`，无符号 20 位，`table_size=2048`）：

1. 算位宽：`W = 0+0+20 = 20`；`B = log2ceil(2048) = 11`；`OffsetBits_c = 20-11 = 9`。
2. 写余数格式：`RemFmt_c = (0, 9-20, 20) = (0, -11, 20)`，总位宽 \(0+(-11)+20 = 9\) 位 ✓。
3. 验证余数值域：无符号 `(0,-11,20)` 的可表示范围是 \([0,\;2^{-11}-2^{-20}) = [0,\;\Delta)\)，其中 \(\Delta = 1/2048 = 2^{-11}\) 恰为段宽 ✓。
4. 算索引格式：`IdxFmt_c = (0, 0+0, 20-20-(-11)) = (0,0,11)`，总位宽 11 = `B` ✓。

**需要观察的现象**：余数 9 位 + 索引 11 位 = 20 位，正好拼回输入位宽，没有重叠也没有遗漏——这就是「拆分」二字的含义。

**预期结果**：`addr_table_o` 为 11 位、`IdxFmt_c` 也是 11 位，二者天然吻合，无需再截位。

#### 4.2.5 小练习与答案

**练习 1**：若把 sin18b 的 `table_size_g` 从 2048 改成 4096，`RemFmt_c` 与 `IdxFmt_c` 各自怎样变化？

> **答**：`B = log2ceil(4096) = 12`；`OffsetBits_c = 20-12 = 8`；`RemFmt_c = (0, 8-20, 20) = (0,-12,20)`（余数缩到 8 位，段宽 \(\Delta\) 减半，精度更高）；`IdxFmt_c = (0,0,12)`（索引变 12 位）。总位数仍为 20，只是「索引多 1 位、余数少 1 位」。

**练习 2**：为什么 `data_table_i` 要把 gradient 放高位、offset 放低位，而不能反过来？

> **答**：可以反过来，但**必须两侧一致**。VHDL 用 `OffsRng_c`（低）/`GradRng_c`（高）切片，生成器用 `gradStr & offsStr`（grad 高、offs 低）拼接——二者约定相同所以对得上。若只改一侧，查出来的 offset/gradient 就会互换，结果完全错误。这是接口契约，不是数学必然。

---

### 4.3 offset + gradient × reminder 计算流水线

#### 4.3.1 概念说明

拆好索引与余数后，剩下的事就是把 4.1 的公式 \(\hat f(x) = o_k + g_k \cdot \text{reminder}\) 算出来。reminder 是「相对段中心」的有符号偏移，所以这一节先解决一个关键技巧：**怎样把无符号余数 \([0,\Delta)\) 变成有符号的「相对中心」偏移 \([-\Delta/2,+\Delta/2)\)**。

答案是 `RemFmt_c` 与 `RemFmtSigned_c` 之间那个**最高位取反 (MSB inversion)** 技巧。在二进制补码里：

- 对一个值域为 \([0,\Delta)\) 的无符号余数，其最高位权值为 \(\Delta/2 = 2^{\text{RemFmt.I}-1}\)。
- **把最高位取反，再按有符号补码重新解释**，等价于把原值整体减去 \(\Delta/2\)。

即「MSB 取反 + 有符号重解释」≡「减去 \(\Delta/2\)」≡「参考点从段左端搬到段中心」。这一步让 offset 表存 \(f(c_k)\) 成为可能（4.1.1）。VHDL 用位翻转实现，Python 用减法实现——**两条路在位层面完全等价**，这是位真的关键一环。

之后就是标准的三级定点流水：**乘法 → 加法 → 末端舍入/饱和**。注意中间两级（乘、加）刻意用 `trunc/wrap`（不舍入、不饱和），把全部精度预算留到末端 `resize`——这是 u2-l2 讲过的「Manual Splitting」范式，目的是让乘加恰好落进一个 DSP slice。

#### 4.3.2 核心流程

整个内核是一条 7 级流水（valid 向量 `Vld(0 to 6)`，见 [hdl/psi_fix_lin_approx_calc.vhd:63](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L63)）。数据走向：

```text
Stage 0  输入打拍                          r.In_0
Stage 1  resize 出索引 IdxFmt_c            r.TblIdx_1  --> addr_table_o（组合输出）
         resize 出余数 RemFmt_c + MSB取反   r.Reminder(1)
Stage 2  外部表的「注册读」延时            （由包装实体的 p_table 占用，内核留空）
Stage 3  切出并打拍表输出                  r.Offs(3), r.Grad_3   <-- data_table_i
Stage 4  乘法 gradient×reminder(Signed)   r.GradVal_4   (IntFmt_c, 全精度)
Stage 5  加法 offset+GradVal              r.Add_5       (AddFmt_c, trunc+wrap)
Stage 6  末端 resize 到 out_fmt_g          r.Out_6       (round+sat) --> dat_o
```

各级结果格式同样由位增长规则严格推出（两侧同构）：

- **乘积格式** `IntFmt_c`：有符号 reminder `[1, R_i-1, R_f]` × gradient `[Gs, Gi, Gf]`，按 `[1,a,b]×[1,c,d]=[1,a+c+1,b+d]` 得 `[1, (R_i-1)+Gi+1, R_f+Gf] = [1, R_i+Gi+1, R_f+Gf]`。VHDL 见 [:51](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L51)，Python 见 [:168-169](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L168-L169)。
- **求和格式** `AddFmt_c`：两数相加，小数位对齐取大、整数位取大再 +1。VHDL 见 [:52](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L52)，Python 见 [:170](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L170)。

#### 4.3.3 源码精读

**Stage 1：拆索引 + 拆余数 + MSB 取反**（本讲最关键的一段）：

[hdl/psi_fix_lin_approx_calc.vhd:95-100](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L95-L100)

```vhdl
v.TblIdx_1                        := psi_fix_resize(r.In_0, in_fmt_g, IdxFmt_c);
v.Reminder(1)                     := psi_fix_resize(r.In_0, in_fmt_g, RemFmt_c);
-- Inverts MSB to have a signed offset
v.Reminder(1)(v.Reminder(1)'high) := not v.Reminder(1)(v.Reminder(1)'high);
```

第三行把余数最高位取反——结合后续 Stage 4 把它当 `RemFmtSigned_c`（有符号）来乘，整体效果就是「减去段宽的一半」。Python 侧直接写成减法，殊途同归：

[model/psi_fix_lin_approx.py:205](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L205) —— `tblRem = psi_fix_resize(inp, inFmt, remFmt) - 2**(remFmt.i-1)`，注释同样写 `#Invert MSB to have signed offset`。

**Stage 2 留空**——故意为外部表的「注册读」延时占一拍：

[hdl/psi_fix_lin_approx_calc.vhd:102-103](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L102-L103) 注释 `Reserved for Table output registers`。包装实体里的 ROM 是「时钟沿打拍输出」的（[model/snippets/psi_fix_lin_approx_tmpl.vhd:85-90](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lin_approx_tmpl.vhd#L85-L90)），这一拍必须算进 valid 对齐，所以内核预留了这一级。

**Stage 3：从总线切出并打拍 offset/gradient**：

[hdl/psi_fix_lin_approx_calc.vhd:105-108](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L105-L108)

```vhdl
v.Offs(3) := data_table_i(OffsRng_c);   -- 低 psi_fix_size(offs_fmt_g) 位
v.Grad_3  := data_table_i(GradRng_c);   -- 紧邻其上的 psi_fix_size(grad_fmt_g) 位
```

**Stage 4：乘法**（reminder 此时按有符号格式 `RemFmtSigned_c` 参与运算）：

[hdl/psi_fix_lin_approx_calc.vhd:110-114](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L110-L114) —— `psi_fix_mult(r.Grad_3, grad_fmt_g, r.Reminder(3), RemFmtSigned_c, IntFmt_c)`，注释 `Reinterpret as signed, equal to python MSB inversion` 点明了与 Python 的位等价关系。

**Stage 5：加法**（`trunc/wrap`，不留舍入预算给 DSP）：

[hdl/psi_fix_lin_approx_calc.vhd:116-120](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L116-L120) —— 注释 `at full precision and without round/sat to fit into DSP slie`（原文拼写），即故意不在加法级量化。

**Stage 6：末端 resize**（把所有精度预算集中在这里 round+sat）：

[hdl/psi_fix_lin_approx_calc.vhd:122-125](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L122-L125) —— `psi_fix_resize(r.Add_5, AddFmt_c, out_fmt_g, psi_fix_round, psi_fix_sat)`。

**Python 侧同样的五步**（黄金模型，结构与 VHDL 逐级对应）：

[model/psi_fix_lin_approx.py:204-215](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L204-L215) —— 依次是取余数并减中心（=MSB 取反）、查 offset、查 gradient、乘（到 `intFmt`）、加（到 `addFmt`，`trunc/wrap`）、末端 resize（`round/sat`）。读这一段时请逐行与上面 Stage 1/4/5/6 对照。

**两段式与流水封装**（复用 u3-l3 的范式）：

[hdl/psi_fix_lin_approx_calc.vhd:79-88](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L79-L88) —— 组合进程开头 `v := r`，然后用切片赋值把 `Vld / Reminder / Offs` 三条流水整体前移一拍。valid 链 `v.Vld(low+1..high) := r.Vld(low..high-1)` 与数据同步平移，驱动末端 `vld_o <= r.Vld(6)`（[:129](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L129)）。时序进程 [:139-147](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L139-L147) 只打拍 + 同步复位 valid。

#### 4.3.4 代码实践

**实践目标**：完整跟踪一个样本「从输入位到输出位」的全过程，亲眼看到 `offset + gradient×reminder` 在定点下成立。**用 sin18b、输入相位 \(x=0\)**（最简单，可手算）。

**已知（来自生成的表 [hdl/psi_fix_lin_approx_sin18b.vhd:38](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_sin18b.vhd#L38)）**：第 0 项 `gradient = to_signed(1608, 12)`、`offset = to_signed(804, 20)`；`gradFmt=(1,3,8)`、`offsFmt=(1,0,19)`、`outFmt=(1,0,17)`、`inFmt=(0,0,20)`、`table_size=2048`。

**操作步骤（手算跟踪）**：

1. **输入**：\(x=0\) → 20 位无符号输入整数 = 0。
2. **Stage 1 索引**：`resize(0, (0,0,20), IdxFmt_c=(0,0,11))` = 0 → 查第 0 项。
3. **Stage 1 余数**：`resize(0, (0,0,20), RemFmt_c=(0,-11,20))` = 9 位的 `0`；MSB 取反 → `0b1_0000_0000` = 256；按 `RemFmtSigned_c=(1,-12,20)` 当有符号读 → \(256 - 512 = -256\)，数值 \(=-256\cdot 2^{-20}=-2^{-12}\approx -0.000244\)（即 \(-\Delta/2\)，相对第 0 段中心的偏移）。
4. **Stage 4 乘法**：`IntFmt_c=(1, -11+3+1, 20+8)=(1,-7,28)`。梯度数值 \(=1608\cdot 2^{-8}\approx 6.281\)（恰为 \(\sin\) 在 \(x\!=\!1/4096\) 处的导数 \(\approx 2\pi\)）。乘积 \(=1608 \times (-256) = -411648\)，LSB \(2^{-28}\)，数值 \(\approx -0.001534\)。
5. **Stage 5 加法**：`AddFmt_c=(1, max(-7,0)+1, max(28,19))=(1,1,28)`。offset 数值 \(=804\cdot 2^{-19}=804\cdot 2^{9}\cdot 2^{-28}=411648\cdot 2^{-28}\)。求和 \(=(411648 + (-411648))\cdot 2^{-28}=0\)。
6. **Stage 6 resize**：`resize(0, AddFmt, (1,0,17), round, sat)` = 0。

**需要观察的现象**：在 \(x=0\) 处，offset 项（段中心函数值 \(\approx 2\pi/4096\)）与梯度项（把直线从段中心拉回原点）**精确抵消**，输出 = 0 = \(\sin(0)\)。这正体现了「offset 存段中心值、reminder 是相对中心的偏移」的设计意图。

**预期结果**：`dat_o` 的 18 位输出 = 0。也由此可推：当输入恰为某个**段中心**时，reminder 数值 = 0，输出 = offset = \(f(c_k)\)；输入偏离中心越多，gradient 项贡献越大。

> 若本地已配置 en_cl_fix + SciPy（**待本地验证**），可用 `psi_fix_lin_approx(psi_fix_lin_approx.CONFIGS.Sin18Bit).Approximate(np.array([0.0]))` 直接得到模型输出，与上面手算的 0 比对。

#### 4.3.5 小练习与答案

**练习 1**：为何 Stage 4 的乘法把 reminder 当成 `RemFmtSigned_c`（有符号）而不是原来的无符号 `RemFmt_c`？如果忘了这一步会怎样？

> **答**：offset 表存的是**段中心**函数值，所以偏移量必须相对中心来量，即有符号的 \([-\Delta/2,+\Delta/2)\)。若错误地用无符号 reminder（值域 \([0,\Delta)\)）去乘，相当于把参考点当成段左端点，而 offset 却是段中心值，二者参考点不一致，整条近似曲线会被整体平移 \(\Delta/2\)，输出错误。`RemFmtSigned_c` 与 `RemFmt_c` 位宽相同，只是把最高位当符号位，所以「MSB 取反 + 当有符号读」一并完成了「换参考点」。

**练习 2**：Stage 5 的加法为什么用 `trunc/wrap`，而把 `round/sat` 全留到 Stage 6？

> **答**：这是 u2-l2 的 Manual Splitting：让乘法与加法都在**全精度**下进行（不中途量化），舍入/饱和只在末端 resize 一次发生，既避免中间截断误差累积，又让「乘 + 加」在综合时落进单个 DSP slice（注释明说 `to fit into DSP slie`）。末端一次 round+sat 既保精度又防溢出。

**练习 3**：从输入 `vld_i` 拉高到输出 `vld_o` 拉高，延迟几个时钟？

> **答**：7 个时钟。valid 向量 `Vld(0 to 6)` 共 7 级，Stage 0 锁存 `vld_i` 到 `Vld(0)`，每拍整体右移一位，末端取 `Vld(6)` 输出。其中 Stage 2 那一拍是留给外部 ROM 的注册读延时的。

## 5. 综合实践

**任务**：换一个配置 **sqrt18b**，重走一遍 4.3.4 的端到端跟踪，把「函数无关内核 + 不同表」这条主线彻底走通。

**背景**（[model/psi_fix_lin_approx.py:87-95](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L87-L95)）：`inFmt=(0,0,20)`、`outFmt=(0,0,17)`（无符号）、`offsFmt=(0,0,19)`、`gradFmt=(0,0,10)`、`points=512`、`validRange=(0.25, (1-2^-17)^2)`、函数 \(f(x)=\sqrt{x}\)。

**操作步骤**：

1. **算位宽**：`W=20`、`B=log2ceil(512)=9`、`OffsetBits_c=11`、`RemFmt_c=(0, 11-20, 20)=(0,-9,20)`（11 位，值域 \([0,2^{-9})=[0,\Delta)\)，段宽 \(\Delta=1/512\)）。
2. **取一个段中心输入**：第 0 段中心 \(c_0 = \Delta/2 = 1/1024\)，此时 reminder（取反后）= 0，输出应**等于 offset[0]** \(=\sqrt{1/1024}=1/32=0.03125\)。
   - 验证位表示：\(1/32\) 在 `outFmt=(0,0,17)` 下 = \(2^{-5}\)，整数位模式 = \(2^{17-5}=2^{12}=4096\)。读生成的 `psi_fix_lin_approx_sqrt18b.vhd` 第 0 项的 offset 字段，应当正是 4096 附近（**待本地核对**，因表是 `psi_fix_from_real` 量化结果，可能有 ±1 LSB）。
3. **偏离中心验证**：取 \(x=0.25\)（恰是 `validRange` 下界，也是某段边界）。说明此时 reminder 处于「最负」端，gradient 项把输出从段中心值拉到边界值，应满足 \(\hat f(0.25)\approx 0.5\)。
4. **核对表接口**：`data_table_i` 宽 \(=\text{size(offsFmt)}+\text{size(gradFmt)}=20+10=30\) 位；`addr_table_o` 宽 9 位。确认 [hdl/psi_fix_lin_approx_calc.vhd:38-39](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_calc.vhd#L38-L39) 的公式对 sqrt18b 也成立。

**需要观察的现象**：尽管 sqrt 与 sin 的「形状」完全不同（一个有符号一个无符号、点数不同、梯度量级不同），**`psi_fix_lin_approx_calc` 的 RTL 一行都没改**——只是包装实体换了表和几个 generic。这就是「函数无关内核」的力量。

**预期结果**：在第 0 段中心输入下，输出 \(\approx 1/32\)；在 `validRange` 边界 \(x=0.25\) 下，输出 \(\approx 0.5\)；全程由同一份计算内核完成。

**回归层面（选做）**：阅读 [sim/config.tcl:169-192](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L169-L192)，确认 sin/sqrt/gaussify/inv 四个测试台各自由 Python 模型 `GenerateTb()` 预生成的 `stimuli.txt` / `response.txt`（[model/psi_fix_lin_approx.py:299-341](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_lin_approx.py#L299-L341)）驱动，测试台模板 [model/snippets/psi_fix_lin_approx_tb_tmpl.vhd:92-115](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lin_approx_tb_tmpl.vhd#L92-L115) 用 `ApplyTextfileContent` 喂激励、`CheckTextfileContent` 逐位比对响应——这正是 u3-l2 协同仿真套路在「生成式组件」上的复用。

## 6. 本讲小结

- **分段线性近似**把任意连续函数拆成 \(P\) 段，每段存一对 (offset=段中心函数值, gradient=段中心导数)，运行时只做 \(\hat f(x)=o_k+g_k\cdot\text{reminder}\)；误差随段宽 \(\Delta\) **平方**下降。
- **索引/余数分解**靠「取输入高 \(B\) 位当地址、其余低位当余数」完成，**零除法**；全部格式由 `in_fmt_g / table_size_g` 用位宽算式推导，函数无关。
- **表接口契约** `addr_table_o`（出地址）/ `data_table_i`（回 gradient&offset 拼接总线）让「计算内核」与「函数表」彻底解耦，于是**一个内核 + N 张表 = N 个函数组件**。
- **MSB 取反 + 有符号重解释** ≡ **减去段宽的一半**，把无符号余数 \([0,\Delta)\) 转成「相对段中心」的有符号偏移 \([-\Delta/2,+\Delta/2)\)——VHDL 用位翻转、Python 用减法，**位等价**，是位真的关键。
- **7 级流水**：输入→拆索引/余数→（外部 ROM 注册读）→切表项→乘→加（trunc/wrap）→末端 resize（round/sat）；中间两级不量化以落进 DSP，这是 Manual Splitting 范式。
- **两侧格式同构**：VHDL 的 `RemFmt_c/IdxFmt_c/IntFmt_c/AddFmt_c` 与 Python 的 `remFmt/idxFmt/intFmt/addFmt` 用**同一组公式**，是「实现各异、模型唯一」的印章。

## 7. 下一步学习建议

本讲只讲了「计算内核」`psi_fix_lin_approx_calc`，那张表是怎么来的还没展开。建议接着读：

- **u8-l2 Python 代码生成器**：讲 `model/psi_fix_lin_approx.py` 的 `GenerateEntity()` 如何读模板 [model/snippets/psi_fix_lin_approx_tmpl.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/snippets/psi_fix_lin_approx_tmpl.vhd)、替换占位符、把 offset/gradient 量化成 `to_signed(...)` 数组，一键生成 sin18b/sqrt18b/inv18b/gaussify20b 一整个组件族——以及 LUT 代码生成 `psi_fix_lut_tmpl`。
- **u8-l3 sqrt / inv / pol2cart_approx**：把 sqrt、1/x 的迭代/近似实现与本讲的查表式线性近似做对比，理解「非线性函数在 FPGA 上有哪几条实现路线、各自怎么选」。

巩固练习：试着仿照 `CONFIGS.Sin18Bit` 写一个新的 `psi_fix_lin_cfg_settings`（例如 \(\tan\) 或 \(\log\)），先不改 RTL，仅用 Python 模型的 `Analyze()` 估算需要多少 `points` 才能达到 1 LSB 精度，体会「点数 ↔ 误差 ↔ 表深」三者间的设计权衡。
