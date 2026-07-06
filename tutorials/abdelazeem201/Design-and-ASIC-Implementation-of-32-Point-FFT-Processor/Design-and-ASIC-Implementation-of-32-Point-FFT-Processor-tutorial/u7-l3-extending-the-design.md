# 设计扩展：从 32 点到参数化

## 1. 本讲目标

前面六单元我们已经把这颗 32 点 FFT 处理器「从算法到版图」完整走了一遍。本讲是整套手册的收官篇，目标是把读者从「读懂一台固定机器」提升到「会改造、会扩展这台机器」。

学完本讲你应该能够：

1. 说清 FFT 点数 \(N\) 与流水线级数 \(S\)、各级延时深度、旋转因子个数、位反转位宽之间的解析关系，并能据此算出 64/128 点所需的资源清单。
2. 掌握 `shift_N` 与 `ROM_N` 两族模块的「宽度公式」与「计数阈值公式」，能动手新增一个 `shift_32` 与 `ROM_32`。
3. 能用 `SIM/twiddle_gen.py` 重新生成新点数所需的旋转因子，并把它写进新 ROM。
4. 识别本设计的可二次开发扩展点：顶层例化、位反转排序表、`parameter`/宏参数化、流水线寄存器插入、折叠与展开。

本讲的隐含结论是一个好消息：**这套 SDC（单路延迟换向器）架构是高度模块化、可堆叠的**。把 32 点扩成 64 点时，原有 10 个子模块里有 8 个可以原样复用，真正要新增的只有 2 个（`shift_32`、`ROM_32`）加上顶层接线和排序表的扩位。

## 2. 前置知识

本讲是 advanced 阶段的收官，默认你已掌握前置讲义的关键结论（下表只列结论，不重复推导）：

| 来自讲义 | 你需要记住的结论 |
|---|---|
| u2-l1 radix-2 DIF | 32 点 FFT = 5 级蝶形；DIF 先加减、后乘旋转因子；输出天然位反转乱序 |
| u2-l2 旋转因子 | \(W_N^k=\cos(2\pi k/N)-j\sin(2\pi k/N)\)；定点 ×256（8 位小数）；24 位补码存 ROM；脚本生成 22 位属原型，ROM 才是交付件 |
| u2-l3 位反转 | 位反转位宽 = \(\log_2 N\)（32 点为 5 位），做两次回到原值 |
| u3-l1 顶层 | 输入 12 位符号扩展到 24 位、数值 ×256；末端取 `out_r[23:8]` 截位还原 |
| u3-l2 radix2 | 蝶形单元为纯组合、自身无 \(N\) 依赖，靠外部 `state` 分时复用（waiting/first half/second half） |
| u3-l3 shift_N | 超宽寄存器移位实现 FIFO 延时，延时深度 = 寄存器位宽/24 |
| u3-l4 ROM_N | ROM 身兼两职：查表输出旋转因子 + 分段生成 2 位 `state`；阈值 N 逐级减半 |
| u4-l1 流水线 | valid 菊花链驱动；第 5 级无 ROM、旋转因子常数化为 256+j0 |

两个本讲会反复用到的术语：

- **级（stage）**：一次 radix-2 蝶形分解。一台 \(N\) 点 FFT 有 \(S=\log_2 N\) 级。
- **延时深度（delay depth）**：每级 `shift_N` 把「差」延迟的拍数，等于该级蝶形的配对距离。

## 3. 本讲源码地图

本讲围绕「参数化」这一主题，引用下列真实源码文件：

| 文件 | 作用 | 本讲用来讲什么 |
|---|---|---|
| [RTL/FFT.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v) | 顶层模块 | 看清 5 级例化的「可堆叠」结构、第 5 级常数化特例、排序表 `case(y_1)`，从而推出 64 点的接线改动 |
| [RTL/shift_16.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_16.v) | 最深的延时线 | 提取「N×24 位寄存器 + 顶部 24 位窗口」的通用模板，生成 `shift_32` |
| [RTL/shift_1.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_1.v) | 最浅的延时线（退化情形） | 验证 N=1 时公式仍成立 |
| [RTL/ROM_16.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_16.v) | 单计数器风格 ROM（旋转因子 + state） | 提取「等待/前半/后半」三段阈值公式，生成 `ROM_32` |
| [RTL/radix2.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v) | 蝶形单元 | 确认它是点数无关的纯组合，扩点数时**无需改动** |
| [SIM/twiddle_gen.py](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/twiddle_gen.py) | 旋转因子生成脚本 | 改两行即可生成 \(W_{64}\) 旋转因子 |

## 4. 核心概念与源码讲解

### 4.1 级数与点数的关系

#### 4.1.1 概念说明

radix-2 FFT 的本质是「分治」：把一个 \(N\) 点 DFT 对半拆成两个 \(N/2\) 点 DFT，每拆一次就是一级蝶形。把 \(N\) 一直拆到 1，需要的拆分次数就是流水线级数：

\[
S = \log_2 N
\]

本项目 \(N=32\)，故 \(S=5\)，对应 [RTL/FFT.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v) 里例化的 `radix_no1` ~ `radix_no5` 五个蝶形。

每级还有两个量随 \(N\) 变化：

- **延时深度** \(d_k\)：第 \(k\) 级的 `shift_N` 要把「差」延迟多少拍，等于该级蝶形的配对距离（组大小的一半）。
- **旋转因子个数**：恰好与延时深度相等（这一点在 4.3 节展开）。

#### 4.1.2 核心流程

第 \(k\) 级（\(k=1..S\)）的延时深度、寄存器宽度、旋转因子个数满足：

\[
d_k = \frac{N}{2^{k}},\qquad W_k^{\text{(reg)}} = d_k \times 24,\qquad \#\text{twiddle}_k = d_k
\]

把所有级的延时深度加起来，就是流水线从第一个样本进到第一个有效样本出的「填充时延」（这与 u4-l1 实测的「延时线深度之和 31」完全吻合）：

\[
\sum_{k=1}^{S} d_k = \frac{N}{2}+\frac{N}{4}+\cdots+1 = N-1
\]

由此可直接列出 32 点与 64 点的对照：

| 级 \(k\) | 32 点（\(S=5\)）延时深度 \(d_k\) | 64 点（\(S=6\)）延时深度 \(d_k\) |
|---|---|---|
| 1 | 16 | **32**（新增） |
| 2 | 8 | 16 |
| 3 | 4 | 8 |
| 4 | 2 | 4 |
| 5 | 1（常数化，无 ROM） | 2 |
| 6 | — | 1（常数化，无 ROM） |
| 填充时延合计 | 31 | 63 |

注意 64 点的第 1 级延时深度是 32，比 32 点最深的 16 还要深，这就是为什么 64 点必须新增一个 `shift_32`；而原 `shift_16/8/4/2/1` 全部能在 64 点的某一级复用。

#### 4.1.3 源码精读

打开 [RTL/FFT.v:L14-L23](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L14-L23)，可以看到顶层用 10 条 `include` 拉入 1 个蝶形 + 5 个 shift + 4 个 ROM。第 5 级没有 ROM，因为它的旋转因子被常数化了——见 [RTL/FFT.v:L230-L243](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L230-L243) 的 `radix_no5` 实例把 `.w_r(24'd256), .w_i(24'd0)` 直接写死（定点 1+j0），这正是 DIF 最后一级旋转因子恒为 1 的体现。

每级的「蝶形 + shift + ROM」三件套以固定模板堆叠，例如第 1 级 [RTL/FFT.v:L95-L126](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L95-L126)：`radix_no1` 的 `delay` 喂 `shift_16`，`shift_16` 的 `dout` 回流成 `radix_no1` 的 `din_a`，`ROM_16` 送 `state` 与旋转因子。把这个模板复制一份、把下标和深度改一下，就是新的一级——这正是「可堆叠」的含义。

#### 4.1.4 代码实践

**实践目标**：用本节的公式，亲手推算 128 点 FFT 的资源清单，验证你真的掌握了 \(N\to\) 级数/延时/填充时延的换算。

**操作步骤**：

1. 计算 128 点的级数 \(S=\log_2 128\)。
2. 逐级列出延时深度 \(d_k\)（从 \(N/2\) 到 1）。
3. 求填充时延总和，验证等于 \(N-1\)。
4. 标注哪几级需要新增 shift/ROM、哪几级可复用现有模块。

**预期结果**（请先自己算再对照）：

- \(S=7\) 级；延时深度序列 64, 32, 16, 8, 4, 2, 1；填充时延 127。
- 需新增 `shift_64`、`shift_32`、`ROM_64`、`ROM_32`；可复用 `shift_16/8/4/2/1` 与 `ROM_16/8/4/2`；第 7 级常数化无 ROM。

> 待本地验证：填充时延是否真的等于仿真里第一个 `out_valid` 相对第一个 `in_valid` 的拍数差，需在改完 RTL 后用 testbench 实测确认。

#### 4.1.5 小练习与答案

**Q1**：把点数从 32 翻倍到 64，级数增加几？填充时延增加几？

答：级数 \(5\to 6\)，增加 1；填充时延 \(31\to 63\)，增加 32（正好是新第 1 级的延时深度）。

**Q2**：为什么延时深度序列是「逐级减半」而不是别的规律？

答：因为 radix-2 每级把蝶形配对距离减半：第 1 级配对相距 \(N/2\) 的样本，第 2 级相距 \(N/4\)，……，配对距离就是 `shift_N` 必须延迟的拍数，所以 \(d_k=N/2^k\)。

---

### 4.2 shift/ROM 的参数化改造

#### 4.2.1 概念说明

要在 64 点设计里新增最深的 `shift_32` 和最大的 `ROM_32`，先得把现有 5 个 shift 和 4 个 ROM 的「宽度/阈值」抽象成公式。一旦有了公式，新增任意点数的模块就是套模板填数字，不再需要重新设计。

`radix2.v` 是点数无关的纯组合逻辑（端口全是 24 位，没有任何 \(N\) 参数），**扩点数时完全不用动它**——这一点请先记住，我们把精力集中在 shift 和 ROM 两族上。

#### 4.2.2 核心流程

**shift_N 族的宽度公式**（来自 u3-l3 的「超宽寄存器移位」结构）：

\[
\text{寄存器位宽} = 24N,\qquad \text{dout 窗口} = [\,24N-1\,:\,24(N-1)\,]
\]

每拍执行 `(tmp_reg << 24) + din`，把新样本拼到最低 24 位，`dout` 固定取最高 24 位读出最老样本，等价于深度为 \(N\) 的无指针 FIFO。

**ROM_N 族的阈值公式**（来自 u3-l4 的「分段计数器」）：

\[
\text{等待段}=[0,N),\quad \text{前半段}=[N,2N),\quad \text{后半段}=[2N,3N)
\]

即 `count<N` → `state=2'b00`（waiting）；`N≤count<2N` → `state=2'b01`（first half）；`2N≤count<3N` → `state=2'b10`（second half）。`ROM_16` 用单计数器直接套这个分段；`ROM_8/4/2` 用 `count`+`s_count` 双计数器实现同样的分段（功能等价）。

#### 4.2.3 源码精读

**shift 族宽度**：`shift_16` 用 [RTL/shift_16.v:L24-L27](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_16.v#L24-L27) 的 `reg [383:0]`（\(384=16\times24\)），`dout` 取 [RTL/shift_16.v:L31-L32](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_16.v#L31-L32) 的 `[383:360]`（即 \([24\times16-1:24\times15]\)）。最小的 `shift_1` 退化成 [RTL/shift_1.v:L24-L27](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_1.v#L24-L27) 的 `reg [23:0]`、`dout` 取 [RTL/shift_1.v:L31-L32](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_1.v#L31-L32) 的 `[23:0]`，公式 \(24\times1=24\) 仍成立。所有 shift 的左移拼接写法完全一致，例如 [RTL/shift_16.v:L44-L45](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/shift_16.v#L44-L45) 的 `(tmp_reg_r<<24) + din_r`。

**ROM 族阈值**：`ROM_16` 在 [RTL/ROM_16.v:L45-L51](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_16.v#L45-L51) 用 `count<16` / `16..32` / `32..48` 三段，正好对应 \(N=16\) 的 \([0,N)/[N,2N)/[2N,3N)\)。旋转因子在 [RTL/ROM_16.v:L52-L138](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_16.v#L52-L138) 的 `case(count)` 里按 `count=32..47` 输出 16 个 \(W_{32}\) 因子（4.3 节细讲）。`ROM_2` 的阈值在 [RTL/ROM_2.v:L48-L56](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_2.v#L48-L56) 同样是 `count<2`，公式一致。

#### 4.2.4 代码实践

**实践目标**：套用宽度/阈值公式，写出 64 点新增模块 `shift_32` 和 `ROM_32` 的关键参数，不动其它任何模块。

**操作步骤**：

1. **`shift_32`**：复制 `shift_16.v`，按下表替换三处：

   | 位置 | `shift_16` 原值 | `shift_32` 新值 | 依据 |
   |---|---|---|---|
   | `shift_reg_r/i`、`tmp_reg_r/i` 位宽 | `[383:0]` | `[767:0]` | \(24\times32=768\) |
   | `dout` 窗口 | `[383:360]` | `[767:744]` | \([24N-1:24(N-1)]\) |
   | 计数器名/位宽 | `counter_16 [5:0]` | `counter_32 [5:0]` | 仅改名（计数器为冗余信号，位宽随意） |

2. **`ROM_32`**：复制 `ROM_16.v`，按下表替换：

   | 位置 | `ROM_16` 原值 | `ROM_32` 新值 | 依据 |
   |---|---|---|---|
   | state 三段阈值 | `16 / 32 / 48` | `32 / 64 / 96` | \([0,N)/[N,2N)/[2N,3N)\) |
   | `case(count)` 因子区间 | `32..47`（16 个） | `64..95`（32 个） | 后半段输出 \(d_k\) 个因子 |

   旋转因子数值由 4.3 节的 `twiddle_gen.py` 生成，这里先留位置。

**需要观察的现象**：改完后用 `diff shift_16.v shift_32.v` 应该只看到位宽常量与计数器名的差异，逻辑结构完全相同——这就是参数化的判据。

**预期结果**：`shift_32` 寄存器 768 位、`dout=[767:744]`；`ROM_32` 三段阈值 32/64/96、查表区间 64..95。> 待本地验证：综合后 `shift_32` 的触发器数应为 `shift_16` 的约 2 倍。

#### 4.2.5 小练习与答案

**Q1**：为什么 `shift_N` 用一条超宽寄存器左移，而不是用 RAM + 读写指针？

答：超宽寄存器移位实现的是「同时读写、无指针」的 FIFO，每拍拼一个新样本进最低位、从最高位读最老样本，延时恰为 \(N\) 拍；省去指针管理，时序干净，代价是寄存器面积随 \(N\) 线性增长（这正是 u7-l2 里「存储大户」的由来）。

**Q2**：`ROM_8` 用 `count`+`s_count` 双计数器，`ROM_16` 用单 `count`，两者描述的 state 分段是否等价？

答：等价。`ROM_16` 的 `count` 直接跨 \(3N\) 拍分段；`ROM_8/4/2` 用 `count` 判等待段、用 `s_count` 在进入前半段后另计 \(2N\) 拍，最终 state 时序与单计数器版本一致，只是写法不同。

---

### 4.3 旋转因子重新生成

#### 4.3.1 概念说明

每一级的旋转因子集合不同。对 DIF 而言，第 \(k\) 级使用 \(M=N/2^k\) 个旋转因子，角距为 \(2\pi/(N/2^{k-1})\)，即第 \(k\) 级存的是 \(W_{\,N/2^{k-1}}^{\,j}\)（\(j=0..M-1\)）：

| 级 \(k\) | 因子总数 \(M\) | 旋转因子集合 | 32 点设计里对应 |
|---|---|---|---|
| 1 | \(N/2\) | \(W_N^{0..N/2-1}\) | `ROM_16` 存 \(W_{32}^{0..15}\) |
| 2 | \(N/4\) | \(W_{N/2}^{0..N/4-1}\) | `ROM_8` 存 \(W_{16}^{0..7}\) |
| … | … | … | … |
| \(S\) | 1 | \(W_2^0=1\) | 常数化，无 ROM |

所以点数翻倍时，**最细的那级旋转因子集合要换**：32 点第 1 级存 16 个 \(W_{32}\)；64 点第 1 级要存 32 个 \(W_{64}\)（角距更细，从 \(\pi/16\) 变 \(\pi/32\)）。

#### 4.3.2 核心流程

生成新旋转因子的三步（定点口径必须与现网一致，否则数值会对不齐）：

1. **算三角值**：\(W_{64}^{j}=\cos(2\pi j/64)-j\sin(2\pi j/64)=\cos(\pi j/32)-j\sin(\pi j/32)\)，\(j=0..31\)。
2. **定点量化**：实部虚部分别 ×256 取整（保留 8 位小数），与 `ROM_16` 现有因子同口径。
3. **转 24 位补码**：负数 \(−x\) 的 24 位补码为 \(2^{24}-x\)，高位补符号位。注意 `twiddle_gen.py` 现在生成的是 22 位、负数用符号-幅值（见 u2-l2 的工程发现），所以**脚本只作原型校验，真正写进 `ROM_32` 的必须是 24 位真补码**，可直接对照 `ROM_16` 里 `6'd33` 那种 `24'b ...` 格式手工/脚本套写。

#### 4.3.3 源码精读

[SIM/twiddle_gen.py:L40-L46](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/twiddle_gen.py#L40-L46) 现在的循环是 `for i in range(16)`、角距 `math.pi/16*i`、缩放 `*256`。把 16 改成 32、把 `math.pi/16` 改成 `math.pi/32`，就得到 64 点第 1 级所需的 32 个 \(W_{64}\) 因子的整数值。

[SIM/twiddle_gen.py:L53-L55](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/twiddle_gen.py#L53-L55) 把整数转成 `'022b'` 22 位串——如前所述这只作校验。真正要写进 `ROM_32` 的是 24 位补码，参考 `ROM_16` 里 [RTL/ROM_16.v:L58-L62](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_16.v#L58-L62) 的 `6'd33` 项 `w_r = 24'b 00000000_00000000_11111011`（即 251，对应 \(\cos(\pi/16)\times256\approx 251\)），可见交付口径是 24 位、低 8 位为小数。

#### 4.3.4 代码实践

**实践目标**：改两行 Python，生成 64 点第 1 级的 32 个旋转因子整数表，并与现网 \(W_{32}\) 做包含关系校验。

**操作步骤**：

1. 复制 `SIM/twiddle_gen.py` 为 `twiddle_gen_64.py`（写在讲义目录或本地实验目录，**不要覆盖原脚本**）。
2. 把第 40 行 `for i in range(16):` 改为 `for i in range(32):`，把第 41–42 行的 `math.pi / 16 * i` 改为 `math.pi / 32 * i`（实部 cos、虚部 \(-\)sin 保持不变）。
3. 同步把第 36–39、50–51 行的 `range(16)` 改成 `range(32)`。
4. 运行 `python3 twiddle_gen_64.py`，观察打印的 `r`、`im` 两个长度 32 的整数列表。

**需要观察的现象**：

- \(j=0\) 必为 `r=256, im=0`（对应 \(W_{64}^0=1+j0\)）。
- 新表的前 16 项（\(j=0..15\)，角距 \(\pi/32\)）应**介于**旧 \(W_{32}\) 表（角距 \(\pi/16\)）的相邻两项之间，且新表的偶数项 \(j=0,2,4,...\) 应**恰好等于**旧 \(W_{32}\) 表的 \(j=0,1,2,...\)（因为 \(W_{64}^{2m}=W_{32}^{m}\)）。

**预期结果**：新表长度 32、首项 256+j0；偶数下标项与 `ROM_16` 现有 \(W_{32}\) 值一一相等。> 待本地验证：把偶数项整数与 [RTL/ROM_16.v:L52-L97](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/ROM_16.v#L52-L97) 中 `count=32,34,...` 的低 8 位逐个比对。

#### 4.3.5 小练习与答案

**Q1**：为什么 64 点第 1 级的旋转因子个数是 32 而不是 64？

答：radix-2 蝶形的「减法×旋转因子」分支只用到 \(W_N^{0..N/2-1}\)，因为 \(W_N^{k+N/2}=-W_N^k\)，后半周期的因子只是前半的取反，可由蝶形加/减分支天然吸收，所以每级只需存 \(N/2\) 个（第 \(k\) 级即 \(N/2^k\) 个）。

**Q2**：如果把量化比例从 ×256 改成 ×512（10 位小数），整个设计要同步改哪些地方？

答：旋转因子、输入符号扩展后的左移量、`radix2` 截位 `mul[31:8]` 都建立在「×256 / 除 256」自洽的定点尺度上（见 u3-l1/u3-l2）。改 ×512 意味着输入要左移 10 位、`radix2` 末尾要截 `mul[33:10]`、数据通路位宽要相应加宽——这是一处牵动多处的改动，非必要不动。

---

### 4.4 二次开发扩展点

#### 4.4.1 概念说明

前三节解决了「点数怎么扩」。本节把视角拉高，列出本设计所有可二次开发的扩展点，并给出 32→64 的完整改造清单。扩展点分四类：

1. **顶层接线扩位**：增减一级就要增减一组 radix2+shift+ROM 三件套，并把 valid 菊花链接长一节。
2. **位反转排序表扩位**：`result` 数组容量、`y_1` 位宽、`case(y_1)` 查表项数都随 \(\log_2 N\) 变化。
3. **参数化封装**：把硬编码的 5 级、24 位、12/16 位等魔法数字提成 `parameter` 或宏，做成可配置 IP。
4. **架构级改造**：插流水线寄存器提频、折叠（复用单 PE 跑多级）省面积、展开（多路并行）提吞吐。

#### 4.4.2 核心流程

**32→64 顶层接线**（在 [RTL/FFT.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v) 上动手）：

1. 在 `include` 区追加 `shift_32.v`、`ROM_32.v`（[RTL/FFT.v:L14-L23](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L14-L23)）。
2. 在原 `radix_no1` 之前插入新的第 1 级 `radix_no1'(shift_32, ROM_32)`，原 5 级顺延为第 2~6 级。
3. valid 菊花链接长：新第 1 级的 `outvalid` 喂第 2 级的 `shift_16/ROM_16.in_valid`，依此类推（参考现有 [RTL/FFT.v:L143-L159](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L143-L159) 的 `radix_no1_outvalid` 接法）。
4. 原第 5 级（常数化）顺延为第 6 级：把 `no5_state`/`r4_valid`/`s5_count` 逻辑（[RTL/FFT.v:L292-L298](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L292-L298)）改成 `no6_state`/`r5_valid`/`s6_count`，`r5_valid` 取新第 5 级（带 `ROM_2` 的那级）的 outvalid 打一拍。

**位反转排序表扩位**（在 [RTL/FFT.v:L37-L60](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L37-L60) 与 [RTL/FFT.v:L313-L458](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L313-L458) 上动手）：

- `result_r/i[0:31]` → `[0:63]`；`y_1` 由 5 位升 6 位；`count_y` 由 6 位升 7 位；`y_1_delay` 由 5 位升 6 位。
- `case(y_1)` 从 32 项（`5'd0..5'd31`）扩成 64 项（`6'd0..6'd63`），写入索引按 u4-l2 的结论 `slot = bitrev_6(y_1) − 1` 重新计算。
- `over` 置位点从 `y_1==31` 改成 `y_1==63`（见现网 [RTL/FFT.v:L450-L454](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L450-L454)）。

**架构级改造（选讲）**：

- **插流水线寄存器提频**：u6-l3 测得关键路径 9.68 ns 压线 100 MHz、slack=0，提频余量为零。可在 `radix2` 的 3 乘 5 加中间插一拍寄存器，把组合深度砍半，换取更高时钟，代价是吞吐延迟 +1。
- **折叠省面积**：把多级蝶形时分复用到同一个 PE（本项目 SDC 本就是单路单 PE 思路的延伸），用时间换面积，适合面积受限、吞吐要求不高的场景。
- **展开提吞吐**：反之，把单路扩成多路并行（如 SDC → MDC），牺牲面积换吞吐，适合宽带通信。

#### 4.4.3 源码精读

**蝶形无需改动**：[RTL/radix2.v:L14-L27](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L14-L27) 的端口全是 24 位、与 \(N\) 无关；它靠外部 `state` 决定行为（[RTL/radix2.v:L37-L81](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/radix2.v#L37-L81) 的 `case(state)`）。无论 32 点还是 64 点，每个 `radix_noX` 实例的代码一字不改——这是本设计最可贵的复用性。

**valid 菊花链模板**：[RTL/FFT.v:L143-L150](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L143-L150) 把上一级 `radix_no1_outvalid` 同时接到下一级 `shift_8` 和 `ROM_8` 的 `in_valid`，这就是新增一级时要照抄的接线范式。

**排序表结构**：[RTL/FFT.v:L324-L456](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L324-L456) 的大 `case(y_1)` 是一张「序号 → 位反转写入槽」的查找表，扩展到 64 点时这张表要从 32 行长到 64 行，是改造工作量最大的部分（建议用脚本按 `bitrev_6` 自动生成，避免手写出错）。

#### 4.4.4 代码实践

**实践目标**：产出一份「32 点扩展为 64 点」的完整改造清单，作为动手前的施工图纸。

**操作步骤**：按下表逐项填空并自查（答案见「预期结果」）。

| 改造项 | 32 点现状 | 64 点目标 | 是否新增文件 |
|---|---|---|---|
| 级数 \(S\) | 5 | ? | — |
| 各级延时深度 | 16/8/4/2/1 | ? | — |
| 新增 shift 模块 | — | shift_?（位宽?） | 是 |
| 新增 ROM 模块 | — | ROM_?（阈值?） | 是 |
| 可复用 shift | shift_16/8/4/2/1 | ? | 否 |
| 可复用 ROM | ROM_16/8/4/2 | ? | 否 |
| 第末级常数化 | 第 5 级 256+j0 | 第 ? 级 256+j0 | — |
| `result` 数组 | `[0:31]` ×16bit | ? | — |
| `y_1` 位宽 | 5 | ? | — |
| `case(y_1)` 项数 | 32 | ? | — |
| 旋转因子重生成 | \(W_{32}\)（16 个） | \(W_{?}\)（? 个） | 用脚本 |
| `no5_state` 逻辑 | `r4_valid`+`s5_count` | ? | — |

**预期结果**：

| 改造项 | 64 点目标 |
|---|---|
| 级数 | 6 |
| 各级延时深度 | 32/16/8/4/2/1 |
| 新增 shift | `shift_32`（768 位，`dout=[767:744]`） |
| 新增 ROM | `ROM_32`（阈值 32/64/96，查表 64..95） |
| 可复用 shift | `shift_16/8/4/2/1` 全部 |
| 可复用 ROM | `ROM_16/8/4/2` 全部 |
| 第末级常数化 | 第 6 级 256+j0 |
| `result` 数组 | `[0:63]` ×16bit |
| `y_1` 位宽 | 6 |
| `case(y_1)` 项数 | 64 |
| 旋转因子 | \(W_{64}\)（32 个，角距 \(\pi/32\)） |
| 末级 state 逻辑 | `no6_state = f(r5_valid, s6_count)` |

> 待本地验证：清单中所有「?」填好后，建议先用 `SIM/FFT.py` 改成 64 点浮点参考模型生成新黄金数据，再改 RTL、跑 `FFT_tb.v`（数据集长度改为 64），以 SNR≥40 dB 收口。

#### 4.4.5 小练习与答案

**Q1**：把点数扩到 64 后，原 `radix2.v` 要不要改？为什么？

答：不用改。`radix2` 是点数无关的纯组合蝶形，端口全 24 位、行为只由外部 `state` 决定，与 \(N\) 解耦，所以扩点数只需在顶层多例化一个它、配齐对应的 shift/ROM 即可。

**Q2**：位反转排序表为什么不能直接沿用 32 点的 32 行 `case`？

答：因为位反转位宽 = \(\log_2 N\)。32 点是 5 位反转（32 行），64 点是 6 位反转（64 行），样本数也翻倍。沿用旧表会导致后半样本无槽可写、写入索引也错位，SNR 必然不通过。

**Q3**：如果只想把内部数据通路从 24 位加宽到 32 位以提升精度，影响面有多大？

答：影响面很大、且很集中：所有 `shift_N` 的寄存器位宽与左移步长（`<<24`→`<<32`、`dout` 窗口）、`ROM_N` 的因子位宽、`radix2` 的 `inter/mul_r/mul_i` 位宽与截位（`[31:8]`→新口径）、顶层输入符号扩展与输出截位都要同步改。相比之下，「扩点数」只动 shift/ROM/排序表，「加宽位宽」几乎动所有模块——后者成本高得多。

## 5. 综合实践

把本讲四个最小模块串起来，完成一份**「64 点 FFT 处理器改造方案书」**（只做纸面设计与脚本，不要求一次跑通版图）：

1. **资源清单**（用 4.1 公式）：列出 6 级的延时深度、寄存器位宽、旋转因子集合，标注哪些模块复用、哪些新增。
2. **新模块设计**（用 4.2 公式）：写出 `shift_32` 的位宽与 `dout` 窗口、`ROM_32` 的三段阈值与查表区间。
3. **旋转因子生成**（用 4.3 方法）：改 `twiddle_gen.py` 两行，打印 32 个 \(W_{64}\) 整数，并校验偶数项与现网 \(W_{32}\) 相等。
4. **顶层与排序表改动**（用 4.4 清单）：说明 `FFT.v` 要插入的新一级、valid 菊花链接法、`result` 数组与 `case(y_1)` 的扩位方案，以及末级 `no6_state` 的生成逻辑。
5. **验证计划**：先改 `SIM/FFT.py` 为 64 点浮点模型生成黄金数据，再改 RTL，用 `FFT_tb.v`（数据集改 64 样本）以 SNR≥40 dB 收口；最后按 u6 流程重跑综合，比较面积/功耗/关键路径与 32 点版本的差异。

交付物：一份 Markdown 方案书 + 改造后的 `twiddle_gen_64.py` 输出截图 + 一张「32 点 vs 64 点资源对比表」。

## 6. 本讲小结

- 点数 \(N\) 决定一切：级数 \(S=\log_2 N\)、第 \(k\) 级延时深度 \(d_k=N/2^k\)、填充时延 \(N-1\)、位反转位宽 \(\log_2 N\)。
- `shift_N` 与 `ROM_N` 都有现成公式：寄存器位宽 \(=24N\)、`dout=[24N-1:24(N-1)]`；ROM 三段阈值 \([0,N)/[N,2N)/[2N,3N)\)。
- 旋转因子按级递减：第 \(k\) 级存 \(W_{N/2^{k-1}}\) 共 \(N/2^k\) 个，点数翻倍时只需把 `twiddle_gen.py` 的 `range` 与角距各改一处即可重生成。
- 本设计高度模块化：32→64 只需新增 `shift_32`+`ROM_32` 两个文件，`radix2` 与其余 shift/ROM 全部原样复用，主要工作量在顶层接线和 `case(y_1)` 排序表扩位。
- 二次开发的四个方向：扩/缩点数、加宽位宽、插寄存器提频、折叠/展开调面积-吞吐权衡——其中扩点数成本最低、加宽位宽成本最高。

## 7. 下一步学习建议

本讲是学习手册的收官，后续可沿三个方向继续深耕：

1. **动手做一次 64 点扩展**：按本讲综合实践的方案书，从 `twiddle_gen_64.py` 到 `FFT_tb.v` 全程跑通一遍，这是把整套手册知识内化为工程能力的最好练习。建议从只改 Python 参考模型开始，先确认算法正确，再动 RTL。
2. **深入架构优化**：重读 u7-l1（3 乘 5 加的加法器优化）与 u7-l2（架构取舍），思考如果要把关键路径从 9.68 ns 压到 5 ns（提频到 200 MHz），应该在 `radix2` 哪些位置插流水线寄存器、会引入几拍额外延迟。
3. **参数化封装成 IP**：尝试把 `FFT.v` 里的 5、24、12、16、31 等魔法数字提成 `parameter N, WIDTH`，用 `generate` 循环自动例化 \(\log_2 N\) 级，并用脚本自动生成 `case(y_1)` 排序表，做成一颗可配置点数/位宽的通用 FFT IP——这是从「读懂一个项目」走向「能产出工业级 IP」的进阶之路。

继续推荐阅读的源码：`RTL/FFT.v`（顶层接线的最佳教材）、`SIM/FFT.py`（算法参考模型，改成任意点数最直接）、`SIM/twiddle_gen.py`（旋转因子生成的最小可运行示例）。
