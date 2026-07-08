# fxp_zoom：被全库复用的位宽变换核心

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `fxp_zoom` 这个模块到底在做什么——把一个定点数从 `(WII, WIF)` 格式搬到 `(WOI, WOF)` 格式，且**数值尽量不变**。
- 看懂它内部分两步走：先做**小数位对齐**（`WIF → WOF`，含 ROUND 舍入），再做**整数位对齐**（`WII → WOI`，含溢出饱和）。
- 手算出"截断/扩展/舍入/上溢出/下溢出"五类典型输入的输出，并能用 iverilog 验证。
- 明白为什么 `fxp_add` / `fxp_addsub` / `fxp_mul` / `fxp_div` / `fxp_sqrt` 都要例化 `fxp_zoom`——它就是全库的"位宽对齐 + 舍入 + 溢出"公共原语。

本讲只聚焦一个模块：`fxp_zoom`。它是后续所有运算模块的地基，**吃透它，后面几讲就只剩算法本身**。

## 2. 前置知识

在继续前，请确认你已掌握（详见 u1-l2）：

- 定点数值 \( v = c / 2^{W_F} \)，其中 \(c\) 是把二进制码当作**有符号补码整数**读出的值，\(W_F\) 是小数位宽。
- 参数命名：`WII/WIF` 是输入的整数/小数位宽，`WOI/WOF` 是输出的整数/小数位宽，整数位宽**含 1 位符号位**。
- `ROUND` 参数控制截断小数位时是否四舍五入。

本讲还需要三个补码小常识：

1. **正数的范围**（\(W_{OI}\) 位整数 + \(W_{OF}\) 位小数）最大正值为 \( 2^{W_{OI}-1} - 2^{-W_{OF}} \)，码型是 `0_111..1_111..1`。
2. **负数的最小值**是 \( -2^{W_{OI}-1} \)，码型是 `1_000..0_000..0`。
3. **符号扩展**：把一个有符号数从窄位宽变到宽位宽时，只要把符号位（最高位）向高位复制即可，数值不变。

> 关键直觉：改变小数位宽 = 在二进制码后面"补零"或"砍尾巴"，本质是乘/除 2 的幂；改变整数位宽 = 在二进制码前面"符号扩展"或"砍高位"，砍高位就可能**丢掉有效信息**，也就是溢出。`fxp_zoom` 把这两件事各做一次。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [RTL/fixedpoint.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v) | 全部可综合模块所在。`fxp_zoom` 在文件最开头，是其余模块的依赖。 |
| [SIM/tb_add_sub_mul_div.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v) | 加减乘除的 testbench。它虽然不直接测 `fxp_zoom`，但里面把定点码还原成浮点数再打印的套路，是本讲实践要复用的"万能钥匙"。 |

`fxp_zoom` 在 [RTL/fixedpoint.v:L22-L94](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L22-L94)。整段不到 70 行，但信息密度很高。我们把它拆成两段 `generate` 分别精读。

---

## 4. 核心概念与源码讲解

### 4.1 小数位宽变换（WIF → WOF）与 ROUND 舍入

#### 4.1.1 概念说明

`fxp_zoom` 的第一步只动**小数位**：把输入的 `WIF` 位小数变成输出的 `WOF` 位小数，整数位宽 `WII` 此刻保持不变。这一步产生一个中间变量 `inr`，它的宽度是 `WII+WOF`（整数仍 `WII` 位，小数已变成 `WOF` 位）。

数值上要保证 `inr` 代表的浮点值 ≈ 输入 `in` 代表的浮点值。分三种情况：

| 关系 | 含义 | 操作 |
| :--- | :--- | :--- |
| `WOF < WIF` | 输出小数位**更少** | 砍掉低位 `WIF-WOF` 位 → **截断**，可能舍入 |
| `WOF == WIF` | 小数位相同 | 直接搬，数值不变 |
| `WOF > WIF` | 输出小数位**更多** | 在低位补 `WOF-WIF` 个 0 → **精度扩展**，数值不变 |

只有第一种情况会损失精度，于是 `ROUND` 参数仅在这里生效。

#### 4.1.2 核心流程

设输入码为 \(c_{in}\)（共 `WII+WIF` 位），它代表 \( v = c_{in} / 2^{W_{IF}} \)。我们想要一个 `WII+WOF` 位的码 \(c_{inr}\)，使 \( c_{inr} / 2^{W_{OF}} \approx v \)，即：

\[
c_{inr} \approx c_{in} \cdot 2^{W_{OF}-W_{IF}}
\]

- `WOF > WIF`：\(2^{W_{OF}-W_{IF}}\) 是整数，左移补零即可，**精确**。
- `WOF == WIF`：系数为 1，直接复制，**精确**。
- `WOF < WIF`：系数是 \(2^{-(W_{IF}-W_{OF})}\)，需要除以 \(2^{W_{IF}-W_{OF}}\)，即**右移砍低位**，可能产生误差。`ROUND=1` 时按"四舍五入"补偿半个 LSB。

> 四舍五入怎么做？被砍掉的最高一位（即 `in[WIF-WOF-1]`）正好代表输出里的 \(0.5\) 个 LSB。它为 1 就进位、为 0 就舍弃——这就是"四舍五入"的二进制实现。

#### 4.1.3 源码精读

变量声明（注意 `inr` 的宽度是 `WII+WOF`，不是输入宽度）：

[RTL/fixedpoint.v:L36-L39](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L36-L39) —— 声明 `inr`（小数对齐后的中间码）、`ini`（整数部分）、`outi/outf`（输出整数/小数）。

第一段 `generate` 处理小数位，三种分支：

[RTL/fixedpoint.v:L41-L62](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L41-L62) —— 这是 `WOF<WIF` / `WOF==WIF` / `WOF>WIF` 的三分支。

其中 `WOF<WIF` 且 `ROUND=1` 的核心两行（通用情形 `WII+WOF>=2`）：

```verilog
inr = in[WII+WIF-1:WIF-WOF];                                   // 先砍掉低位 = 截断
if(in[WIF-WOF-1] & ~(~inr[WII+WOF-1] & (&inr[WII+WOF-2:0])))   // 四舍五入 + 防溢出
    inr = inr + 1;
```

逐位解释这个看似复杂的 `if` 条件：

- `in[WIF-WOF-1]`：被砍掉那一段的**最高位**，即"0.5 LSB"判定位。
- `inr[WII+WOF-1]`：`inr` 的符号位（最高位）。
- `&inr[WII+WOF-2:0]`：符号位以下的全部位是否都为 1。
- `~inr[...] & (&inr[...])`：**正数且已经是正最大值**（`0_111..1`）。
- 整个条件 = "0.5 位为 1" **并且** 不是"正最大值"。

也就是说：**正常情况下四舍五入（0.5 位为 1 就 +1）；唯独当 +1 会让正最大值翻成负最小值（符号翻转）时，放弃进位**，把那个半个 LSB 吃掉，保住正最大值不溢出。这是一个很小但很关键的"舍入防溢出"保护。

> 当 `WII+WOF==1`（极端窄配置，`inr` 只有 1 位即符号位）时走另一个分支 [RTL/fixedpoint.v:L49-L53](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L49-L53)，逻辑退化，初学可略过。

`WOF==WIF` 与 `WOF>WIF` 两分支很简单：

[RTL/fixedpoint.v:L55-L61](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L55-L61) —— 相等则整段复制；更多则高位放原码、低位补零。

#### 4.1.4 代码实践

**目标**：亲手验证"四舍五入"与"直接截断"的差异。

**操作**：在 4.2 的 testbench 里，把例化参数 `.ROUND(1)` 改成 `.ROUND(0)`，重新仿真，对比 `in=0x0108` 这一组（详见 4.2）的 `out` 是否从 `0x011` 变成 `0x010`。

**预期**：`ROUND=1` 时 `1.03125` 进位成 `1.0625`（out=`0x011`）；`ROUND=0` 时直接截断成 `1.0`（out=`0x010`）。差 1 个 LSB，正是四舍五入的体现。

**待本地验证**：请运行后确认上述差值。

#### 4.1.5 小练习与答案

**练习 1**：`WIF=8, WOF=4`，输入码 `in=0x0108`（即 `1.03125`）。手算 `ROUND=1` 时 `inr` 的值。

**答案**：`inr = in[15:4] = 0x010 = 16`；被砍位 `in[3:0]=0b1000`，0.5 位 `in[3]=1`；`inr=16` 不是正最大值 → 进位 → `inr=17`。`inr` 代表 `17/16 = 1.0625`。

**练习 2**：为什么舍入条件里要专门排除"正最大值"？

**答案**：若 `inr` 已是 `0_111..1`（正最大），再 `+1` 会变成 `1_000..0`（负最小），数值从正最大跳到负最小，符号翻转。排除这一情况可让结果停在正最大值，避免舍入本身引发溢出。

---

### 4.2 整数位宽变换（WII → WOI）与溢出饱和

#### 4.2.1 概念说明

小数位对齐后，`inr` 的整数位宽仍是 `WII`。第二步要把整数位宽变成 `WOI`。这一步用 `{ini, outf} = inr` 把 `inr` 拆成整数部分 `ini`（`WII` 位）和小数部分 `outf`（`WOF` 位，直接作为输出的小数部分）。然后只对整数部分做位宽变换：

| 关系 | 含义 | 操作 |
| :--- | :--- | :--- |
| `WOI < WII` | 输出整数位**更少** | 可能丢有效高位 → 检测**溢出**并饱和 |
| `WOI >= WII` | 输出整数位**更多或相等** | 符号扩展，**永不溢出** |

"溢出"分两种：值太大超过正最大叫**上溢出**，太小低于负最小叫**下溢出**。发生时 `overflow=1`，并把 `out` 钳位到对应极值（饱和，saturation）。

#### 4.2.2 核心流程

输出格式 `(WOI, WOF)` 的可表示范围为：

\[
\text{负最小} = -2^{W_{OI}-1}, \qquad \text{正最大} = 2^{W_{OI}-1} - 2^{-W_{OF}}
\]

当 `WOI < WII` 时，对 `ini`（`WII` 位有符号）做判断：

1. **正数且超限**（上溢出）→ `overflow=1`，`out` = 正最大（`outi=011..1`, `outf=111..1`）。
2. **负数且超限**（下溢出）→ `overflow=1`，`out` = 负最小（`outi=100..0`, `outf=000..0`）。
3. **未超限** → `overflow=0`，`outi = ini` 的低 `WOI` 位（因为高位都是符号扩展，可安全丢弃）。

当 `WOI >= WII` 时，直接符号扩展，`overflow` 恒为 0。

#### 4.2.3 源码精读

第二段 `generate`：

[RTL/fixedpoint.v:L65-L90](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L65-L90) —— `WOI<WII` 走带溢出检测的分支；否则走符号扩展分支。

`WOI<WII` 分支的关键判断（`ini[WII-1]` 是符号位）：

```verilog
{ini, outf} = inr;
if         ( ~ini[WII-1] & |ini[WII-2:WOI-1] ) begin   // 正数 + 高位有1 = 上溢出
    overflow = 1'b1;  outi = {WOI{1'b1}};  outi[WOI-1]=1'b0;  outf = {WOF{1'b1}};   // 饱和到正最大
end else if(  ini[WII-1] & ~(&ini[WII-2:WOI-1]) ) begin // 负数 + 高位非全1 = 下溢出
    overflow = 1'b1;  outi = 0;  outi[WOI-1]=1'b1;  outf = 0;                       // 饱和到负最小
end else begin                                          // 未超限
    overflow = 1'b0;  outi = ini[WOI-1:0];
end
```

读懂两个掩码表达式：

- `ini[WII-2:WOI-1]` 是"会被砍掉的那几个高位"（位于符号位 `ini[WII-1]` 与保留位 `ini[WOI-2:0]` 之间）。
- 对**正数**而言，这些位理应全为 0（符号扩展）；只要有一个 1（`|...` 为真），说明数值大到放不进 `WOI` 位 → 上溢出。
- 对**负数**而言，这些位理应全为 1（符号扩展）；只要不全为 1（`~(&...)` 为真），说明数值小到放不进 `WOI` 位 → 下溢出。

`WOI>=WII` 的符号扩展分支很直白：

[RTL/fixedpoint.v:L83-L89](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L83-L89) —— 先按符号位把 `outi` 填全 1 或全 0，再把低 `WII` 位覆盖成 `ini`，`overflow` 恒 0。

最后拼回输出：

[RTL/fixedpoint.v:L92](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L92) —— `assign out = {outi, outf};`

#### 4.2.4 代码实践

**目标**：单独例化 `fxp_zoom`，用一个最小 testbench 一次性观察"舍入、上溢出、下溢出、正常"四类行为。

**配置**：`WII=8, WIF=8, WOI=6, WOF=4`。输出小数位更少（`4<8`）→ 强制截断+舍入；输出整数位更少（`6<8`）→ 可能溢出。输入范围 `[-128, 128)`，输出范围 `[-32, 32)`，所以喂 ±100 必然溢出。

**操作步骤**：在 `SIM/` 下新建一个 `tb_zoom.v`（**示例代码**，仓库中原本没有），同时编译 `fixedpoint.v`：

```verilog
`timescale 1ps/1ps
module tb_zoom;
    localparam WII=8, WIF=8, WOI=6, WOF=4;
    reg  [WII+WIF-1:0] in = 0;
    wire [WOI+WOF-1:0] out;
    wire               overflow;

    fxp_zoom #(.WII(WII),.WIF(WIF),.WOI(WOI),.WOF(WOF),.ROUND(1)) u (
        .in(in), .out(out), .overflow(overflow));

    task show;
        input [WII+WIF-1:0] _in;
        begin
            #10000 in = _in;
            #10000 $display("in=%h out=%h of=%b | in_val=%f out_val=%f",
                in, out, overflow,
                $signed(in )*1.0/(1<<WIF),
                $signed(out)*1.0/(1<<WOF));
        end
    endtask

    initial begin
        show('h0108);  // +1.03125 : 触发舍入（0.5 位=1）
        show('h0104);  // +1.015625: 触发截断（0.5 位=0）
        show('h6400);  // +100.0   : 触发上溢出
        show('h9C00);  // -100.0   : 触发下溢出
        show('hFF00);  // -1.0     : 正常（不溢出）
        $finish;
    end
endmodule
```

编译运行（参照仓库 `.bat` 的写法）：

```bash
iverilog -g2001 -o sim.out tb_zoom.v ../RTL/fixedpoint.v
vvp -n sim.out
```

**需要观察的现象与预期结果**（已按本讲源码手算，**仍待本地验证**）：

| 输入码 | 输入值 | 预期 out | 预期 out 值 | overflow | 说明 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `0x0108` | +1.03125 | `0x011` | +1.0625 | 0 | 四舍五入进位 |
| `0x0104` | +1.015625 | `0x010` | +1.0 | 0 | 0.5 位=0，截断 |
| `0x6400` | +100.0 | `0x1FF` | +31.9375 | 1 | 上溢出→饱和到正最大 |
| `0x9C00` | -100.0 | `0x200` | -32.0 | 1 | 下溢出→饱和到负最小 |
| `0xFF00` | -1.0 | `0x3F0` | -1.0 | 0 | 正常，数值不变 |

其中正最大 `0x1FF = 0b01_1111_1111`（`outi=011111, outf=1111`）、负最小 `0x200 = 0b10_0000_0000`（`outi=100000, outf=0000`），与 4.2.2 的公式完全对上。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `WOI>=WII` 时 `overflow` 恒为 0？

**答案**：输出整数位宽不少于输入，输出可表示范围 ⊇ 输入范围，任何输入值都放得下，不可能溢出，所以只需符号扩展。

**练习 2**：若把配置改成 `WOI=10, WOI>=WII`，输入 `-100.0 (0x9C00)` 的 `out` 会是多少？`overflow` 呢？

**答案**：走符号扩展分支：`out` 整数部分符号扩展为 `10` 位，小数部分 `outf` 为 `inr` 低 `WOF` 位。数值仍是 `-100.0`，`overflow=0`。可见"加宽整数位"永远不会溢出。

**练习 3**：上溢出饱和时为什么 `outf` 也要设成全 1，而不是 0？

**答案**：正最大值是 \( 2^{W_{OI}-1} - 2^{-W_{OF}} \)，需要整数部分 `011..1` **且** 小数部分 `111..1` 才能达到这个"最接近上限"的值；若 `outf=0`，结果会比真正的正最大小将近 1，不是最优饱和。

---

### 4.3 fxp_zoom 如何被全库复用

#### 4.3.1 概念说明

定点运算有一个普遍难题：**两个操作数的小数位宽往往不同，运算结果的位宽又和输入都不一样**。比如乘法，积的小数位宽 = 两路输入小数位宽之和；加法要先对齐小数点。如果每个运算模块都自己手写一遍"对齐、舍入、饱和"的位操作，代码会重复且易错。

`fxp_zoom` 把这套通用逻辑抽出来，于是所有运算模块都**例化它**来负责位宽收放：输入侧用它做对齐（`ROUND=0`，因为对齐不该引入额外舍入），结果侧用它做收敛（`ROUND` 按用户配置）。这就是为什么 README 把它列为"位宽变换"基础模块，且"有溢出、舍入控制"。

#### 4.3.2 核心流程

以 `fxp_mul` 为例（最简洁，只例化 1 个）：

1. 直接用 `$signed(ina) * $signed(inb)` 得到**全精度积** `res`，其整数位宽 `WRI=WIIA+WIIB`、小数位宽 `WRF=WIFA+WIFB`。
2. 例化一个 `fxp_zoom`，把 `(WRI, WRF)` 的 `res` 收敛到用户要的 `(WOI, WOF)`，由它负责舍入与溢出。

加法/加减法/除法/开方同理，区别只在"用了几个 `fxp_zoom`"和"中间位宽怎么定"。

#### 4.3.3 源码精读

- **`fxp_mul`**：只例化 1 个 `fxp_zoom` 做结果收敛。积位宽 `WRI=WIIA+WIIB, WRF=WIFA+WIFB` 见 [RTL/fixedpoint.v:L293-L296](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L293-L296)，例化见 [RTL/fixedpoint.v:L298-L308](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L298-L308)。

- **`fxp_add`**：例化 3 个。两个输入各用一个 `fxp_zoom` 对齐到公共位宽（`ROUND=0`），见 [RTL/fixedpoint.v:L134-L156](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L134-L156)；加完结果再用一个收敛回 `(WOI,WOF)`，见 [RTL/fixedpoint.v:L158-L168](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L158-L168)。注意加法特意把中间整数位宽设为 `WRI=WII+1`（[L127](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L127)），多 1 位防止两数相加本身溢出。

- **`fxp_div`**：例化 2 个，把被除数、除数都先放大到统一的中间位宽 `(WRI,WRF)` 再做逐位除法，见 [RTL/fixedpoint.v:L433-L455](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L433-L455)。

- **`fxp_sqrt`**：例化 1 个，把开方结果 `resushort` 收敛到 `(WOI,WOF)`，见 [RTL/fixedpoint.v:L734-L744](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L734-L744)。

> 一个共同模式：**输入侧的 `fxp_zoom` 一律 `ROUND=0`**（对齐只移位、不额外舍入），**只有结果侧的 `fxp_zoom` 才把用户传进来的 `ROUND` 透传**。理解这一点，以后看任何运算模块都能立刻分清"哪个 zoom 在舍入"。

#### 4.3.4 代码实践

**目标**：用源码阅读的方式，验证"输入侧不舍入、结果侧才舍入"这一模式。

**操作步骤**：

1. 打开 [RTL/fixedpoint.v:L134-L168](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L134-L168)（`fxp_add` 的三个例化）。
2. 数一下：`ina_zoom`、`inb_zoom` 的 `.ROUND(0)` 还是 `.ROUND(ROUND)`？`res_zoom` 呢？
3. 再看 `fxp_addsub`（[L214-L260](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L214-L260)）和 `fxp_mul`（[L298-L308](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L298-L308)），确认同样规律。

**预期**：所有"输入对齐"用的 `fxp_zoom` 都是 `.ROUND(0)`；所有"结果输出"用的 `fxp_zoom` 都是 `.ROUND(ROUND)`。

**待本地验证**：用编辑器搜索 `fxp_zoom #` 与 `.ROUND`，逐个核对。

#### 4.3.5 小练习与答案

**练习 1**：`fxp_add` 为什么要把中间整数位宽设成 `WRI=WII+1`，而不是直接用 `WII`？

**答案**：两个 `WII` 位的数相加，结果最多需要 `WII+1` 位整数才不丢进位。先扩展 1 位再交给结果侧 `fxp_zoom`，把"加法本身的进位"和"输出范围溢出"分开处理，避免加法进位被误判。

**练习 2**：`fxp_mul` 的积整数位宽是 `WIIA+WIIB`，如果用户把 `WOI` 设得比它小，会发生什么？由谁负责处理？

**答案**：积可能超出输出范围，发生上溢出/下溢出；这个判断和饱和完全由结果侧那个 `fxp_zoom`（`res_zoom`）负责，乘法模块本身只管算全精度积。

---

## 5. 综合实践

把 4.2 的 testbench 扩展成一个**自校验**验证器：

1. 复制 `tb_zoom.v`，增加一个 `integer pass=0, fail=0;`。
2. 用 `for` 循环遍历一批**随机**输入码（例如 `in = $random;`，跑 1000 组）。
3. 对每组，用软件公式算出"理想值" `sw = $signed(in)*1.0/(1<<WIF)`；再用输出格式把 `out` 还原 `hw = $signed(out)*1.0/(1<<WOF)`。
4. 判定规则：
   - 若 `overflow==1`：本组跳过精度比较（饱和值本就不等于理想值），仅记录。
   - 若 `overflow==0`：要求 `|sw - hw|` 不超过 1 个输出 LSB（即 `1.0/(1<<WOF)`）；满足则 `pass++`，否则 `fail++` 并打印该组。
5. 仿真结束打印 `PASS=x FAIL=y`。

**预期**：`FAIL=0`。这同时验证了 `fxp_zoom` 的舍入误差被约束在 ½LSB 内（四舍五入保证），以及溢出饱和不会误报。

> 提示：当 `WOI<WII` 时，部分随机输入会真实溢出（`overflow=1`），这些组不计入精度比较，否则会误报。这也正好帮你体会"溢出"与"精度误差"是两类不同现象。

## 6. 本讲小结

- `fxp_zoom` 把定点数从 `(WII,WIF)` 搬到 `(WOI,WOF)`，分两步：**先小数对齐（`inr`），再整数对齐（`out`）**。
- 小数位 `WOF<WIF` 时砍低位并按 `ROUND` 四舍五入，且对"正最大值"做了防符号翻转保护；`WOF>WIF` 补零、`WOF==WIF` 直拷，都精确。
- 整数位 `WOI<WII` 时检测上溢出（正超限→正最大）/下溢出（负超限→负最小）并饱和；`WOI>=WII` 符号扩展，永不溢出。
- `overflow=1` 时 `out` 钳位到 \( 2^{W_{OI}-1}-2^{-W_{OF}} \)（正最大）或 \( -2^{W_{OI}-1} \)（负最小）。
- 全库 `add/addsub/mul/div/sqrt` 都例化 `fxp_zoom`：输入侧 `.ROUND(0)` 只对齐，结果侧 `.ROUND(ROUND)` 才舍入。
- 用 `$signed(x)*1.0/(1<<W)` 在 testbench 里把码还原成浮点，是验证任意定点模块的万能钥匙。

## 7. 下一步学习建议

- 下一篇 **u2-l1（fxp_add 与 fxp_addsub）**：你会看到 3 个 `fxp_zoom` 如何配合一个 `$signed` 加法完成"对齐→相加→收敛"，是 `fxp_zoom` 最直接的应用。
- 之后 **u2-l2（fxp_mul）**：积位宽 `WIIA+WIIB / WIFA+WIFB` 的推导，配合本讲"结果侧 `fxp_zoom`"的理解，乘法就只剩一行 `$signed` 相乘。
- 想加深舍入/饱和手感，可回头改造本讲 testbench 的 `ROUND` 与 `WOI/WOF` 参数，观察极值附近的行为。
