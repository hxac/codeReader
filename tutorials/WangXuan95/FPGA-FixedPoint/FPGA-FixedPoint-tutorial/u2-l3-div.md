# fxp_div：恢复余数法实现的单周期除法

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `fxp_div` 为什么要把被除数和除数先转成绝对值、最后再补回符号，以及 `sign = 被除数符号 ^ 除数符号` 的来历。
- 逐行读懂核心 `for (shamt=WOI-1; shamt>=-WOF; ...)` 循环，明白它如何用「左移/右移 + 试探 + 累加」实现恢复余数法（restoring division），逐位求出商。
- 理解舍入判定 `acct-divd < divd-acc` 的几何含义：比较「真值到 floor 的距离」与「真值到 ceil 的距离」，取更近者。
- 理解最后一段如何把无符号商补回二进制补码符号，并判定上溢出（饱和到正最大）/下溢出（饱和到负最小）。
- 知道这是一个组合逻辑模块，关键路径极长（ README 标注「单周期版时序不易收敛」），实际工程中应改用下一讲的 `fxp_div_pipe`。

## 2. 前置知识

本讲承接 [u1-l3（fxp_zoom）](./u1-l3-fxp-zoom.md) 和 [u2-l1（fxp_add/fxp_addsub）](./u2-l1-add-sub.md)，默认你已经掌握：

- **定点数换算**：定点码作为有符号补码整数除以 \(2^{W_F}\) 才是真实值，仿真里用 `$signed(code)*1.0/(1<<W)` 还原。
- **fxp_zoom 的作用**：把定点数从 `(WII,WIF)` 格式搬到 `(WOI,WOF)`，能做小数位截断/补零（带 `ROUND` 舍入）和整数位截断（带上溢出/下溢出饱和），是全库的位宽搬运工。
- **统一参数命名**：`WOI/WOF` 是输出整数/小数位宽，`WIIA/WIFA`、`WIIB/WIFB` 是两路输入的整数/小数位宽。

本讲还会用到两个小学算术概念：

- **恢复余数法**：手工做除法时，我们「估一位商、用被除数减去这一位对应的除数倍数，如果不够减就把这一位改成 0（恢复原状）」。硬件里就是把「减法 + 不够减则恢复」逐位做一遍。
- **二进制补码取负**：对一个数取相反数等于「按位取反再加 1」，即 `(~x)+1`。`fxp_div` 反复用到它来去符号和补符号。

一个关键直觉：**除法不像加法/乘法那样有现成的 Verilog 运算符可以用**（`/` 在可综合 RTL 里基本不支持）。所以 `fxp_div` 必须自己用移位和比较把商一位一位「试」出来——这正是本讲的主角。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [RTL/fixedpoint.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v) | 全部可综合模块所在文件。本讲聚焦其中的 `fxp_div`（第 397–497 行），并复用 `fxp_zoom`（第 22–94 行）。 |
| [SIM/tb_add_sub_mul_div.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v) | 同时测试加减乘除四个单周期模块的 testbench，其中 `/` 那一栏就是 `fxp_div` 的输出。 |
| [SIM/tb_add_sub_mul_div_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div_run_iverilog.bat) | 一键仿真脚本：`iverilog -g2001` 编译 + `vvp` 运行。 |

## 4. 核心概念与源码讲解

`fxp_div` 计算 `out = dividend / divisor`，输出为 `(WOI,WOF)` 格式的定点数，并在溢出时给出 `overflow` 信号。它是一个纯组合逻辑（`always @ (*)`）模块，整体可以拆成 4 个最小模块来理解：

1. **整体结构与工作位宽推导**（端口 + `WRI/WRF` 的来历）。
2. **取绝对值**：把有符号除法归约成无符号除法。
3. **恢复余数法**：核心 `for` 循环逐位求商。
4. **舍入、符号恢复与溢出饱和**：把无符号商收拾成最终的有符号定点输出。

### 4.1 fxp_div 的整体结构与工作位宽推导

#### 4.1.1 概念说明

`fxp_div` 的输入是两路**可能格式不同**的定点数（被除数 `dividend` 是 `(WIIA,WIFA)`，除数 `divisor` 是 `(WIIB,WIFB)`），输出是 `(WOI,WOF)`。直接拿这两路不同格式的数相除很不方便，所以模块第一步是把它们都搬到统一的「工作格式 `(WRI,WRF)`」里再做除法。

`(WRI,WRF)` 不能随便取，必须足够宽，否则移位试探时会溢出丢失高位、导致商算错。关键约束有两个：

- 工作格式要能无损容纳被除数与除数 → 整数位宽至少 `max(WIIA, WIIB)`、小数位宽至少 `max(WIFA, WIFB)`。
- 求最高位商时要把除数左移 `WOI-1` 位 → 这会「撑大」整数位宽需求。

#### 4.1.2 核心流程

```
1. 计算工作位宽 WRI / WRF（见 4.1.3 的公式）。
2. 取绝对值：得到无符号的被除数 udividend、除数 udivisor，以及符号 sign。
3. 用 fxp_zoom 把 udividend、udivisor 搬到 (WRI,WRF) → divd、divr。
4. for 循环逐位试探，得到无符号商 out。
5. 舍入（ROUND）。
6. 补回符号、判定溢出饱和。
```

#### 4.1.3 源码精读

模块端口与参数声明，和 `fxp_add`/`fxp_mul` 完全一致的命名约定，只是输入改名为语义更明确的 `dividend`/`divisor`，输出是 `reg` 型（因为要在 `always` 块里逐位写）：

[RTL/fixedpoint.v:397-410](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L397-L410) —— `fxp_div` 的参数与端口：`WIIA/WIFA/WIIB/WIFB` 是两路输入位宽，`WOI/WOF` 是输出位宽，`ROUND` 控制截断舍入。

工作位宽 `WRI/WRF` 的推导是本模块最值得琢磨的两行：

[RTL/fixedpoint.v:414-415](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L414-L415) —— `WRI = max(WOI+WIIB, WIIA)`，`WRF = max(WOF+WIFB, WIFA)`。

直观理解这两个 `max` 的每一项：

- `WRI` 取 `WIIA`：保证被除数的整数部分能无损搬过来（`WIIA <= WRI` 时 `fxp_zoom` 只做符号扩展，不截断）。
- `WRI` 取 `WOI+WIIB`：求商的最高位（`shamt = WOI-1`）时要把除数左移 `WOI-1` 位。除数幅值 \(<2^{WIIB}\)，左移后需要约 \(WIIB + WOI\) 位整数位宽才不溢出累加器。这一项通常占主导。
- `WRF` 取 `WIFA`：保留被除数的小数精度。
- `WRF` 取 `WOF+WIFB`：求商的最低位（`shamt = -WOF`）时要把除数右移 `WOF` 位，为了在右移后仍保留除数原有的 `WIFB` 位小数信息，工作小数位宽至少要 `WOF+WIFB`。

一句话：**`WRI/WRF` 取得足够宽，是为了让整个移位试探过程不丢任何影响结果有效位的精度**。注意两个 `fxp_zoom` 例化都传 `.ROUND(0)`——对齐阶段只搬运、不舍入，把舍入留到最后统一做。

> 说明：上面用「约 \(WIIB+WOI\) 位」做了直觉论证。严格地，因为 `WRI >= WOI+WIIB`，`divr << (WOI-1)` 不会越过 `WRI+WRF` 位的累加器表示范围，从而 `acct = acc + (divr<<shamt)` 不会回绕，第 4.3 节的比较 `acct <= divd` 始终有效。

#### 4.1.4 代码实践

**目标**：手算验证 `WRI/WRF` 的取值，建立对工作位宽的直觉。

1. 打开 [SIM/tb_add_sub_mul_div.v:16-21](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L16-L21)，读出测试用的参数：`WIIA=10, WIFA=11, WIIB=8, WIFB=12, WOI=15, WOF=14`。
2. 代入公式手算：`WRI = max(WOI+WIIB, WIIA) = max(15+8, 10) = 23`；`WRF = max(WOF+WIFB, WIFA) = max(14+12, 11) = 26`。
3. **需要观察的现象**：`WRI/WRF`（23/26）比输入位宽（10/11、8/12）和输出位宽（15/14）都大不少，说明工作格式确实「撑大」了。
4. **预期结果**：你的手算结果应为 `WRI=23, WRF=26`（待本地用仿真打印 `$display("WRI=%d WRF=%d", WRI, WRF)` 确认；`WRI/WRF` 是 `localparam`，可在 testbench 里通过层次路径引用 `fxp_div_i.WRI` 读取）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `WOI` 调大到 32，`WRI` 会变成多少？为什么？
**答案**：`WRI = max(32+WIIB, WIIA) = max(32+8, 10) = 40`。因为求最高位商需要把除数左移 `WOI-1=31` 位，整数位宽需求随之增大，这正是除法「商越宽、组合逻辑越深」的根源。

**练习 2**：为什么两个输入对齐用的 `fxp_zoom` 都传 `.ROUND(0)`？
**答案**：对齐阶段只是把数据无损搬到工作格式（`WRI>=WII*`、`WRF>=WIF*`，只会扩展不会截断），没有任何需要舍入的截断发生；真正需要舍入的是最后商的小数位，那由专门的舍入段处理。统一传 0 也避免了对齐阶段引入额外误差。

### 4.2 取绝对值：把有符号除法归约到无符号除法

#### 4.2.1 概念说明

恢复余数法本质上是**无符号**整数的逐位相除算法，处理「不够减就恢复」的逻辑已经很繁琐，如果再叠加正负号的各种组合（负÷正、正÷负、负÷负），电路会非常复杂。

`fxp_div` 的策略很优雅：**先把被除数和除数都变成正数（绝对值），用无符号算法求出「商的幅值」，最后再根据两数符号的异或决定要不要把商取负**。这把 4 种符号组合归约成了 1 种。

#### 4.2.2 核心流程

```
sign = 被除数最高位(符号位) ^ 除数最高位(符号位)   // 1 表示结果为负
若 被除数为负: udividend = (~dividend) + 1        // 补码取负 = 取反加1
否则:          udividend = dividend
除数同理得到 udivisor
```

符号判定用异或：同号得正（`0`），异号得负（`1`），和十进制乘除法的「同号正、异号负」完全一致。

#### 4.2.3 源码精读

[RTL/fixedpoint.v:427-431](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L427-L431) —— 取符号 + 取绝对值。

要点：

- `sign = dividend[WIIA+WIFA-1] ^ divisor[WIIB+WIFB-1]`：两路输入的最高位就是各自的符号位，异或得到结果符号。
- `udividend = dividend[...] ? (~dividend)+ONEA : dividend`：负数走 `(~x)+1` 取补码负值（即绝对值），正数直接用。`ONEA` 是宽度为 `WIIA+WIFA` 的常量 1（[第 423-425 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L423-L425)），保证加 1 时位宽匹配、不会因为字面量 `1` 被截断。

随后用两个 `fxp_zoom` 把绝对值搬到工作格式（注意 `.ROUND(0)`）：

[RTL/fixedpoint.v:433-455](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L433-L455) —— `dividend_zoom` 和 `divisor_zoom`，分别输出 `divd` 和 `divr`。它们的 `overflow` 端口悬空（因为对齐只扩展不截断，必然不会溢出）。

#### 4.2.4 代码实践

**目标**：验证 4 种符号组合下 `sign` 与绝对值处理是否正确。

1. 在 testbench 的 `initial` 块里追加 4 条 `test(...)` 调用，覆盖：`+ ÷ +`、`+ ÷ -`、`- ÷ +`、`- ÷ -`（可复用已有的 `'h6e56e35e`(正) / `'h9432d234`(负) 之类向量）。
2. 运行 `tb_add_sub_mul_div_run_iverilog.bat`。
3. **需要观察的现象**：`/` 那一栏打印的 `HW-result` 符号应满足「同号得正、异号得负」，且幅值只取决于被除数幅值除以除数幅值。
4. **预期结果**：4 组的 `HW-result` 与 `SW-result`（testbench 用真实浮点除法算的参考值）误差在 1 LSB 以内（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sign` 用 `^`（异或）而不是 `!=`？
**答案**：最高位是 1 比特信号，`a ^ b` 在 1 比特语义下等价于「两者不同」，正好对应「异号为负」。用异或更贴近硬件直觉，综合结果就是一个 XOR 门。

**练习 2**：`(~dividend)+ONEA` 中，如果漏掉 `ONEA` 写成 `(~dividend)+1`，什么情况下会出错？
**答案**：Verilog 字面量 `1` 默认位宽很小（通常 32 位或按上下文截断），当 `dividend` 很宽时 `+1` 的进位可能被截断，导致取负错误。`ONEA` 显式声明为 `WIIA+WIFA` 位，保证加法位宽正确。这是定点库反复出现的安全写法（参见 `fxp_addsub` 里的 `ONE`）。

### 4.3 恢复余数法：for 循环逐位试探商

#### 4.3.1 概念说明

现在 `divd`（被除数幅值）和 `divr`（除数幅值）都是工作格式下的无符号定点数，我们要算的是商 `out`。先把目标说清楚：

设 `out_int` 为输出码的整数值，它代表真实商 \(Q_{real} = out\_int / 2^{WOF}\)。而真实商又等于 \(divd_{real} / divr_{real}\)。把它们都用码值（整数）表示，可以推出除法要满足的核心等式：

\[
out\_int \times divr\_int = divd\_int \times 2^{WOF}
\]

恢复余数法就是从最高位到最低位，**逐位决定 `out_int` 的每一个比特**：每试探一位，就检查「把这一位算上后，左边 \(out\_int \times divr\) 会不会超过 \(divd\_int \times 2^{WOF}\)」。超过就把这一位恢复为 0，不超过就保留为 1。

代码没有直接维护 \(out \times divr\)，而是维护一个**累加器 `acc`**，它代表「已经确定的那些位所吃掉的被除数份额」。每来一位，就尝试把 `divr` 移到该位对应的权重上、加进 `acc`，再和 `divd` 比。

#### 4.3.2 核心流程

循环变量 `shamt` 从 `WOI-1` 递减到 `-WOF`，正好对应输出的每一个比特位 `out[WOF+shamt]`（从最高位 `out[WOI+WOF-1]` 到最低位 `out[0]`）。每一位的权重在「真实商」里是 \(2^{shamt}\)。

```
acc = 0
for shamt = WOI-1  down to  -WOF:
    if shamt >= 0:  acct = acc + (divr << shamt)     # 这一位在输出里偏高，divr 左移
    else:           acct = acc + (divr >> (-shamt))  # 这一位在输出小数部分，divr 右移
    if acct <= divd:                                  # 够减：保留这一位
        acc = acct
        out[WOF+shamt] = 1
    else:                                            # 不够减：恢复（不累加，位清0）
        out[WOF+shamt] = 0
```

为什么 `shamt` 会取到负数？因为输出的低 `WOF` 位是小数位，它们在真实商里的权重是 \(2^{-1}, 2^{-2}, \ldots, 2^{-WOF}\)，对应 `shamt = -1 .. -WOF`，这时除数要**右移**而不是左移。

为什么「累加 + 与被除数比」等价于「减法恢复」？因为 `acc` 累加的是「已经吃掉的被除数份额」，条件 `acct <= divd` 等价于「新增这一位后，吃掉的份额没超过被除数」即「够减」。不够减时不执行 `acc = acct`，`acc` 自动保持原值——这就是「恢复」。

> 数学上可以验证：循环结束时 `acc` 满足 \(acc \approx out\_int \times divr\_int / 2^{WOF}\)，且 `acc <= divd_int`，剩余 \(divd - acc\) 就是余数。

#### 4.3.3 源码精读

[RTL/fixedpoint.v:459-471](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L459-L471) —— 恢复余数法主循环。

逐行说明：

- `acc = 0;` 每次组合逻辑求值都从 0 开始累加（这是 `always @ (*)`，不是时序逻辑）。
- `for(shamt=WOI-1; shamt>=-WOF; shamt=shamt-1)`：`shamt` 是 `integer`（[第 457 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L457) 声明），可以取负值，所以循环能覆盖整数位和小数位。
- `out[WOF+shamt]`：把 `shamt` 映射到输出的物理比特下标。`shamt=WOI-1` → `out[WOI+WOF-1]`（最高位），`shamt=-WOF` → `out[0]`（最低位），正好覆盖全部 `WOI+WOF` 位。
- `if(acct <= divd) begin acc = acct; out[...] = 1; end else out[...] = 0;`：够减就累加并置 1，不够减就清 0 且不动 `acc`（恢复）。

注意 `out` 是 `output reg`，循环里每一位都被显式赋值，所以没有「锁存器」隐患。

#### 4.3.4 代码实践

**目标**：用 Python 实现一个最小化的恢复余数除法，理解算法本身（与 RTL 无关，先建立直觉）。

```python
# 示例代码：纯整数恢复余数除法（示意，非项目代码）
def restoring_div(divd, divr, WOI, WOF):
    acc = 0
    out = 0
    for shamt in range(WOI-1, -WOF-1, -1):      # 含 -WOF
        if shamt >= 0:
            acct = acc + (divr << shamt)
        else:
            acct = acc + (divr >> (-shamt))
        if acct <= divd:
            acc = acct
            out |= (1 << (WOF + shamt))
    return out, acc
```

1. 取 `divd=100, divr=3, WOI=8, WOF=0`（纯整数），调用 `restoring_div`，打印 `out, acc`。
2. **需要观察的现象**：`out` 应为 33，`acc` 应为 99（= 33×3），余数 `100-99=1`。
3. **预期结果**：`out=33, acc=99`，与 `100 // 3 = 33` 一致（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：循环为什么是「从 `WOI-1` 到 `-WOF`」而不是反过来？
**答案**：恢复余数法必须从最高位开始试探，因为高位的权重最大，先确定高位才能保证后续低位是在「扣除高位后的余量」上继续试探。从低向高无法工作（低位先填满会立刻超出）。

**练习 2**：`out[WOF+shamt]` 当 `shamt=-WOF` 时下标是多少？对应输出的哪一位？
**答案**：下标是 `WOF+(-WOF)=0`，即输出的最低位 `out[0]`，也就是真实商里权重 \(2^{-WOF}\) 的那一比特（输出的最小精度单位 LSB）。

### 4.4 舍入、符号恢复与溢出饱和

#### 4.4.1 概念说明

经过 4.3 的循环，`out` 是**无符号商的幅值**（因为输入已取绝对值），它是对真实商向下取整（floor）的结果。还需要三步收尾：

1. **舍入**：如果 `ROUND=1`，判断真实商更接近 `out` 还是 `out+1`，必要时加 1。
2. **符号恢复**：若 `sign=1`，把无符号幅值取补码负值变成负数；若 `sign=0`，保持正数。
3. **溢出饱和**：补码后的结果若超出 `(WOI,WOF)` 能表示的范围，分别饱和到正最大（上溢出）或负最小（下溢出），并置 `overflow=1`。

#### 4.4.2 核心流程

**舍入的几何含义**（这是本讲最巧妙的一段）：

真实商落在 `out`（floor）和 `out+1`（ceil，单位是 1 LSB）之间。定义两个距离：

- `divd - acc`：余数 = 真实值**高出** `out` 的部分（离 floor 的距离）。
- `acct - divd`，其中 `acct = acc + (divr>>WOF)`：因为 `divr>>WOF` 正好是「1 个输出 LSB 在被除数尺度下的重量」，所以 `acct` 对应 `out+1`，`acct - divd` 就是真实值**低于** `out+1` 的部分（离 ceil 的距离）。

于是 `acct - divd < divd - acc` 就是「**离 ceil 更近**」，应向最近值舍入，把 `out` 加 1。这是 round-to-nearest，且无需算出真实商即可判定。

**符号恢复与饱和**：输出最高位 `out[WOI+WOF-1]` 是符号位。结果应为正（`sign=0`）时，若幅值已经顶到符号位（`out[WOI+WOF-1]=1`），说明正数溢出 → 上溢出，饱和到正最大 `0111...1`；结果应为负（`sign=1`）时，先看幅值：若幅值未顶到符号位，直接 `(~out)+1` 取负；若顶到了符号位，取负后会突破负最小 → 下溢出，饱和到负最小 `1000...0`（仅当幅值严格大于 \(2^{WOI-1}\) 时才真正溢出，恰好等于时正好是负最小、不溢出）。

#### 4.4.3 源码精读

舍入段：

[RTL/fixedpoint.v:473-477](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L473-L477) —— `ROUND` 舍入。

- `&out` 是缩位与，`(&out)=1` 表示 `out` 全 1（已达该位宽最大值），此时 `~(&out)=0` 跳过舍入，避免 `out+1` 溢出回绕。
- `acct = acc + (divr>>WOF)` 对应 `out+1` 的累加器值，`acct-divd < divd-acc` 即「离 ceil 更近」，`out = out+1`。

符号恢复与溢出饱和段：

[RTL/fixedpoint.v:479-494](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L479-L494) —— 补符号 + 上下溢出饱和。

逐段读：

- `overflow = 1'b0;` 默认不溢出。
- `if(sign)`（结果应为负）：
  - `if(out[WOI+WOF-1])`：幅值已经占用了符号位。
    - `if(|out[WOI+WOF-2:0]) overflow = 1'b1;`：低位只要有一位 1，说明幅值 \(>2^{WOI-1}\)，取负会突破负最小 → 下溢出。
    - `out[WOI+WOF-1]=1; out[WOI+WOF-2:0]=0;`：无论是否溢出都把结果钳成 `1000...0`（负最小）。幅值恰好等于 \(2^{WOI-1}\) 时低位全 0，`overflow` 保持 0，结果正好是负最小，正确。
  - `else`：幅值没占符号位，正常取负 `out = (~out) + ONEO`。
- `else`（结果应为正）：
  - `if(out[WOI+WOF-1])`：正数却占了符号位 → 上溢出，`overflow=1`，钳成正最大 `0111...1`（`out[WOI+WOF-2:0]` 置全 1）。
  - 否则 `out` 已是正确正值，不动。

#### 4.4.4 代码实践

**目标**：构造能分别触发「上溢出」和「下溢出」的除法用例。

1. 选小输出位宽便于触发溢出：把 `fxp_div` 例化为 `WOI=4, WOF=0`（输出只能表示 \([-8,7]\)），输入用较宽格式如 `WIIA=8,WIFA=0,WIIB=8,WIFB=0`（纯整数）。
2. **上溢出**：`dividend = 100, divisor = 1` → 商应为 100，远超 7。
3. **下溢出**：`dividend = -100, divisor = 1` → 商应为 -100，远低于 -8。
4. **需要观察的现象**：上溢出用例 `overflow=1` 且 `out` 钳为 7；下溢出用例 `overflow=1` 且 `out` 钳为 -8（码值 `1000`）。
5. **预期结果**：两组都应 `overflow=1`，`HW-result` 分别为 7 和 -8（待本地验证；可对照 `SW-result` 看它仍是 100/-100，从而确认是饱和而非真实结果）。

#### 4.4.5 小练习与答案

**练习 1**：舍入条件里为什么要先判断 `~(&out)`？
**答案**：`(&out)` 为真表示 `out` 所有位都是 1（已是最大幅值）。若此时再 `out=out+1`，会从全 1 进位成 `100...0`，符号位被错误翻转为负。所以必须跳过舍入。这是一种防回绕保护。

**练习 2**：结果应为负、且幅值恰好等于 \(2^{WOI-1}\)（即 `out[WOI+WOF-1]=1`、低位全 0）时，`overflow` 是多少？为什么？
**答案**：`overflow=0`。因为 \(+2^{WOI-1}\) 取补码负值正好是 \(-2^{WOI-1}\)，即 `(WOI,WOF)` 格式下的负最小，在表示范围内，不算溢出。代码里 `|out[WOI+WOF-2:0]` 为 0，所以不置 `overflow`，并把结果保持为 `1000...0`。

**练习 3**：除数为 0 时（如 `dividend=5, divisor=0`），按代码逻辑 `out` 最终会变成什么？`overflow` 呢？
**答案**：`divr=0`，循环里 `acct=0 <= divd` 恒成立，每一位都置 1，`out` 变全 1；舍入因 `&out` 为真被跳过；`sign=0`（5 和 0 同号）分支里 `out[WOI+WOF-1]=1` → 上溢出，`overflow=1`，`out` 钳成正最大。即「除以 0 → 饱和到正最大并报溢出」（负数除以 0 则走 `sign=1` 分支饱和到负最小）。该行为属于实现定义，建议实际工程里在模块外拦截除数 0。

## 5. 综合实践

**任务**：用 C 或 Python 编写一个与 `fxp_div` 严格对齐的「恢复余数法」参考模型，对随机输入比对 `out` 和 `overflow`，并验证除数为 0 的行为。

参考模型必须按 `fxp_div` 的相同步骤实现，而不是直接用语言自带的 `/` 运算符：

```python
# 示例代码：fxp_div 的参考模型（示意，非项目代码）
def fxp_div_ref(divd_code, divr_code, WIIA,WIFA,WIIB,WIFB,WOI,WOF, ROUND=1):
    def to_signed(c, w):  return c - (1<<w) if c >> (w-1) else c
    da = to_signed(divd_code, WIIA+WIFA)
    db = to_signed(divr_code, WIIB+WIFB)
    sign = (da<0) ^ (db<0)
    da, db = abs(da), abs(db)                      # 取绝对值
    # 真实商（用高精度浮点当作 oracle，仅用于换算回码值）
    Q_real = da / (2**WIFA) / (db / (2**WIFB))
    out_int = int(Q_real * (1<<WOF))               # 取 floor
    # 舍入（与 RTL 同语义：四舍五入到最近）
    if ROUND and out_int != (1<<(WOI+WOF))-1:
        # 用余数判断离 floor/ceil 哪个近
        if (Q_real*(1<<WOF) - out_int) >= 0.5:
            out_int += 1
    # 符号恢复 + 饱和（与 RTL 第 479-494 行同逻辑）
    overflow = 0
    top = 1 << (WOI+WOF-1)
    if sign:
        if out_int & top:
            if out_int & (top-1): overflow = 1
            out_int = top
        else:
            out_int = (~out_int + 1) & ((1<<(WOI+WOF))-1)
    else:
        if out_int & top:
            overflow = 1
            out_int = top - 1
    return out_int, overflow
```

操作步骤：

1. 固定一组位宽参数（直接复用 testbench 的 `WIIA=10,WIFA=11,WIIB=8,WIFB=12,WOI=15,WOF=14`）。
2. 生成若干随机 `divd_code`、`divr_code`，分别调用上面的参考模型得到 `(out_ref, ovf_ref)`。
3. 把同一批向量灌进 [SIM/tb_add_sub_mul_div.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v) 的 `fxp_div_i`（可在 testbench 里把 `odiv`、`odivo` 用 `$display` 打印成十六进制码值，方便和 Python 的整数比对），运行 `tb_add_sub_mul_div_run_iverilog.bat`。
4. **需要观察的现象**：每一组随机输入下，RTL 的 `odiv/odivo` 与参考模型的 `out_ref/ovf_ref` 完全一致。
5. **预期结果**：所有随机用例 `out` 与 `overflow` 全部匹配；额外测试 `divisor=0` 时 RTL 与模型都给出 `overflow=1` 且饱和到极值。若发现个别用例不一致，重点排查参考模型的舍入阈值是否与 RTL 的 `acct-divd < divd-acc` 完全等价（注意 RTL 是按整数余数比较，模型用 `>=0.5` 是等价近似，边界情况可能差 1 LSB，这正是值得用大量随机向量暴露的地方）。

> 提示：参考模型里用浮点 `Q_real` 只是为了方便换算码值；RTL 本身全程用整数移位比较、不依赖浮点。两者在 `out/overflow` 上应当一致，但浮点精度可能引入极少数边界差异——遇到时请回到 RTL 的整数语义判定哪一方正确。

## 6. 本讲小结

- `fxp_div` 是单周期（纯组合逻辑）定点除法，README 明确标注「时序不易收敛」，工程中应优先用下一讲的 `fxp_div_pipe`。
- 它先把被除数/除数取绝对值、记下 `sign = 被除数符号 ^ 除数符号`，把有符号除法归约成无符号除法。
- 工作位宽 `WRI = max(WOI+WIIB, WIIA)`、`WRF = max(WOF+WIFB, WIFA)`，取得足够宽以保证移位试探不丢精度；两个对齐用的 `fxp_zoom` 都传 `.ROUND(0)`。
- 核心 `for(shamt=WOI-1; shamt>=-WOF; ...)` 是恢复余数法：维护累加器 `acc`，逐位试探 `divr<<shamt`（或右移）累加后是否 `<= divd`，够则置 1 累加、不够则恢复。
- 舍入 `acct-divd < divd-acc` 的几何含义是「比较真值到 floor 与到 ceil 的距离，取更近者」，等价于四舍五入，并用 `~(&out)` 防止加 1 回绕。
- 收尾按 `sign` 补回补码符号，并在正数占符号位（上溢出，钳正最大）或负数幅值超 \(2^{WOI-1}\)（下溢出，钳负最小）时置 `overflow=1` 饱和。

## 7. 下一步学习建议

- **流水线化**：本模块的关键路径是 `WOI+WOF` 级串联的「移位 + 加法 + 比较」，正是它「时序不易收敛」的根源。下一讲 [u3-l2 fxp_div_pipe](./u3-l2-div-pipe.md) 会把这个 `for` 循环展开成 `WOI+WOF+3` 级流水线，用数组做级间寄存器逐拍处理一位商，建议对照本讲的循环体阅读，体会「循环变量 → 流水线级」的映射。
- **对比开方**：[u2-l4 fxp_sqrt](./u2-l4-sqrt.md) 的逐位试探思路与本讲非常相似（都是「试探 + 不够则恢复」），但维护的是「部分根」和「平方累计」，对照阅读能加深对这类逐位算法的理解。
- **深入 fxp_zoom**：本讲反复依赖 `fxp_zoom` 做对齐，如果对其截断/舍入/饱和细节还不够熟，回头重读 [u1-l3](./u1-l3-fxp-zoom.md) 的第 41–94 行。
- **仿真方法学**：本讲的「RTL vs 软件参考模型」比对套路，在 [u3-l6 仿真验证方法学](./u3-l6-simulation-testbench.md) 会有系统总结，建议届时把这里的 Python 参考模型改写成 testbench 内的自校验逻辑。
