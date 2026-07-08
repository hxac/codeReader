# fxp_sqrt：逐位求解的开方算法

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `fxp_sqrt` 为什么是单目运算、输入输出位宽如何定制，以及结果整数位宽为什么取 `WRI = WTI/2`（平方根的位数约为原数一半）。
- 从数学上推导逐位开方（bit-by-bit / digit-recurrence square root）的原理：维护「部分根 `resu`」与「它的平方累计 `resu2`」，每试探一位时利用 \((resu + 2^{ii})^2 = resu^2 + 2^{ii+1}\cdot resu + 2^{2ii}\) 增量更新。
- 逐行读懂核心 `for(ii=WRI-1; ii>=-WIF; ...)` 循环，明白 `resu<<(1+ii)` / `resu>>(-1-ii)` 与 `ONET<<(2*ii+WIF)` 三项分别对应平方展开式的哪一项，以及 `inu!=0` 守卫的作用。
- 理解对负数输入的处理：先取绝对值开方，再用 `sign` 把结果取补码负值（即把 \(\sqrt{x}\) 扩展定义为 \(-\sqrt{|x|}\)），最后经 `fxp_zoom` 收敛到输出位宽。
- 知道这是一个纯组合逻辑模块，README 标注「单周期版时序不易收敛」，工程中应改用 [u3-l3 fxp_sqrt_pipe](./u3-l3-sqrt-pipe.md)。

## 2. 前置知识

本讲承接 [u1-l3（fxp_zoom）](./u1-l3-fxp-zoom.md)，默认你已经掌握：

- **定点数换算**：定点码作为有符号补码整数除以 \(2^{W_F}\) 才是真实值；仿真里用 `$signed(code)*1.0/(1<<W)` 还原为浮点。
- **fxp_zoom 的作用**：把定点数从 `(WII,WIF)` 格式搬到 `(WOI,WOF)`，能做小数位截断/补零（带 `ROUND` 舍入）和整数位截断（带上溢出/下溢出饱和）。本讲结尾用它收敛位宽。
- **统一参数命名**：单目运算用 `WII/WIF`（输入）与 `WOI/WOF`（输出）。

本讲还会用到两个基础概念：

- **二进制补码取负**：对整数 \(x\) 取相反数 = 按位取反再加 1，即 `(~x)+1`。`fxp_sqrt` 用它去符号和补符号。
- **完全平方数**：若一个数恰好等于某个整数（或可表示的定点数）的平方，则它的平方根是精确值、没有截断误差。这是本讲用来验证模块正确性的最佳测试输入。

一个关键直觉：**和除法一样，可综合 RTL 里没有现成的开方运算符**。`fxp_sqrt` 不能写 `out = $sqrt(in)`，必须自己用「移位 + 比较 + 累加」把根的每一位「试」出来。它的算法骨架和 [u2-l3 fxp_div](./u2-l3-div.md) 的恢复余数法是表兄弟——都是「从高位到低位逐位试探，够则保留、不够则恢复」，只是维护的量从「商 × 除数」换成了「根的平方」。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [RTL/fixedpoint.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v) | 全部可综合模块所在文件。本讲聚焦 `fxp_sqrt`（第 690–746 行），结尾复用 `fxp_zoom`（第 22–94 行）。 |
| [SIM/tb_fxp_sqrt.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v) | 开方专用 testbench，同时例化单周期 `fxp_sqrt` 与流水线 `fxp_sqrt_pipe`，并打印 `oval^2`（根的平方）方便和输入对比。 |
| [SIM/tb_fxp_sqrt_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt_run_iverilog.bat) | 一键仿真脚本：`iverilog -g2001` 编译 `tb_fxp_sqrt.v` 与 `../RTL/fixedpoint.v`，再 `vvp` 运行。 |

## 4. 核心概念与源码讲解

`fxp_sqrt` 计算 `out = sqrt(in)`，输入是 `(WII,WIF)` 格式的定点数，输出是 `(WOI,WOF)`，并在结果超出输出范围时给出 `overflow`。它是纯组合逻辑（`always @ (*)`），整体可以拆成 4 个最小模块来理解：

1. **整体结构、结果位宽推导与符号预处理**（端口、`WTI/WRI` 的来历、`sign` 与 `inu`）。
2. **逐位开方的数学原理**（`resu` 与 `resu2` 的不变量、平方展开增量公式）。
3. **for 循环逐位试探**（左移/右移/`ONET` 三项、`inu!=0` 守卫）。
4. **结果还原：取补符号 + fxp_zoom 收敛位宽**（`resushort`、唯一的 `fxp_zoom` 例化）。

### 4.1 整体结构、结果位宽推导与符号预处理

#### 4.1.1 概念说明

`fxp_sqrt` 是**单目运算**：只有一路输入 `in (WII,WIF)`、一路输出 `out (WOI,WOF)`。开方把数值「缩小」——一个 \(N\) 位整数能表示到 \(2^N\)，它的平方根只到 \(2^{N/2}\)，所以**平方根的整数位宽约为原数整数位宽的一半**。这是 `fxp_sqrt` 第一处与加减乘除都不同的地方：结果位宽不由输入直接推导，而是「折半」。

模块第一步还要处理符号。实数域里负数没有实平方根，但定点硬件需要给出一个确定行为：`fxp_sqrt` 的约定是「对负数取绝对值开方，再把结果加上负号」，即 \(\text{out} = -\sqrt{|\text{in}|}\)。这样电路只需实现「无符号开方」，符号在最后补回，和 `fxp_div`「先取绝对值、最后补符号」的套路一致。

#### 4.1.2 核心流程

```
1. 推导结果整数位宽：WTI = 把 WII 凑成偶数；WRI = WTI/2。
2. 取符号 sign = in 的最高位；取绝对值 inu（负数走 (~in)+1）。
3. for 循环从高位到低位逐位试探根，得到无符号根 resu 和它的平方累计 resu2。
4. 按 sign 把 resu 收拾成有符号的 resushort（负数取补码负值）。
5. 用 fxp_zoom 把 resushort 从 (WRI+1, WIF) 搬到 (WOI, WOF)，顺带做舍入与溢出饱和。
```

#### 4.1.3 源码精读

模块端口与参数声明。注意它是单目运算，参数只有 `WII/WIF`（输入）和 `WOI/WOF`（输出）加 `ROUND`，比加减乘除少一组输入位宽：

[RTL/fixedpoint.v:690-700](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L690-L700) —— `fxp_sqrt` 的参数与端口：`WII/WIF` 输入整数/小数位宽，`WOI/WOF` 输出整数/小数位宽，`ROUND` 控制截断舍入。

最值得琢磨的两行是结果位宽的推导：

[RTL/fixedpoint.v:702-703](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L702-L703) —— `WTI = (WII%2==1) ? WII+1 : WII;  WRI = WTI/2;`

- `WTI` 把输入整数位宽 `WII` **凑成偶数**（奇数则加 1）。为什么？因为开方要把位数「折半」，整数位宽为偶数才能被 2 整除、避免半个比特的尴尬。
- `WRI = WTI/2` 是**根的整数位宽**。直觉：输入幅值 \(< 2^{WII}\)，其平方根 \(< 2^{WII/2} \le 2^{WRI}\)，所以根用 `WRI` 位整数就够表示。

随后是三个位宽匹配用的「常量 1」（和 `fxp_div` 里的 `ONEA/ONEB` 同一目的，防止字面量 `1` 在宽位宽加法里被截断），以及关键寄存器声明：

[RTL/fixedpoint.v:705-714](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L705-L714) —— `ONEI/ONET/ONER` 三个单位常量；`resushort` 是送给 `fxp_zoom` 的有符号根；`sign` 是符号位；`inu` 是输入绝对值（拓宽到 `WTI+WIF` 位）；`resu` 是逐位搭起来的部分根；`resu2` 是它的平方累计；`resu2tmp` 是试探用的临时量。

符号与绝对值预处理在 `always` 块开头：

[RTL/fixedpoint.v:717-720](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L717-L720) —— `sign = in[WII+WIF-1]`（最高位即符号位）；`inu = sign ? (~in)+ONEI : in`（负数取补码负值得绝对值，正数直接用）。注意 `inu` 先清零再把低 `WII+WIF` 位赋值，等价于把绝对值**零扩展**到 `WTI+WIF` 位——这 1 位（当 `WII` 为奇数时）的余量留给平方累计时不溢出。

#### 4.1.4 代码实践

**目标**：手算验证 `WTI/WRI` 的取值，建立「根位宽折半」的直觉。

1. 打开 [SIM/tb_fxp_sqrt.v:16-19](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v#L16-L19)，读出测试参数：`WII=10, WIF=10, WOI=6, WOF=12`。
2. 代入公式手算：`WII=10` 已是偶数 → `WTI=10` → `WRI=WTI/2=5`。
3. **需要观察的现象**：根的整数位宽 `WRI=5`，加上后续给符号预留的 1 位（见 4.4），送给 `fxp_zoom` 的整数位宽是 `WRI+1=6`，恰好等于测试台的 `WOI=6`——说明这组参数下整数位宽严丝合缝、不会因截断而溢出。
4. **预期结果**：`WTI=10, WRI=5`（可在 testbench 里用层次路径 `$display("WTI=%d WRI=%d", fxp_sqrt_i.WTI, fxp_sqrt_i.WRI)` 打印确认，它们是 `localparam`）。

#### 4.1.5 小练习与答案

**练习 1**：如果 `WII=7`（奇数），`WTI` 和 `WRI` 分别是多少？为什么要把 `WII` 先凑成偶数？
**答案**：`WTI = 7+1 = 8`，`WRI = 8/2 = 4`。把整数位宽凑成偶数是为了让「折半」干净落地：若直接 `WII/2 = 3`，会丢掉半个比特的表示范围，根可能不够宽。凑偶后再除 2，保证 `2·WRI ≥ WII`，根的整数部分一定能容纳 \(\sqrt{}\)' 的结果范围。

**练习 2**：`inu` 为什么先 `inu=0` 再 `inu[WII+WIF-1:0] = ...`，而不是直接 `inu = ...`？
**答案**：`inu` 声明为 `WTI+WIF` 位（可能比 `WII+WIF` 大 1 位）。直接整体赋值会把低 `WII+WIF` 位的值符号扩展或截断到 `WTI+WIF` 位；而先清零再只写低 `WII+WIF` 位，等价于**零扩展**，确保最高位（可能的第 `WTI+WIF-1` 位）是 0，把 `inu` 当作无符号幅值处理。这对后续「平方累计比较」至关重要。

### 4.2 逐位开方的数学原理

#### 4.2.1 概念说明

现在 `inu` 是输入幅值的码值（无符号），我们要逐位求出根 `resu`。先把目标说清楚：设根的码值为 \(R\)（整数），它代表真实根 \(r = R / 2^{WIF}\)；输入码值 \(I =\) `inu`，代表真实值 \(i = I / 2^{WIF}\)。开方要求：

\[
r \le \sqrt{i}
\quad\Longleftrightarrow\quad
r^2 \le i
\quad\Longleftrightarrow\quad
\frac{R^2}{2^{2\cdot WIF}} \le \frac{I}{2^{WIF}}
\quad\Longleftrightarrow\quad
R^2 \le I \cdot 2^{WIF}
\]

把两边都除以 \(2^{WIF}\)，定义**平方累计**：

\[
\text{resu2} \;\triangleq\; \frac{R^2}{2^{WIF}}
\]

那么判定条件就变成朴素的整数比较 **`resu2 ≤ inu`**（即 \(\text{resu2} \le I\)）。这正是代码里那一行 `if(resu2tmp<=inu)`。

逐位开方从最高位到最低位决定 \(R\) 的每一个比特。根的第 \(ii\) 位（\(ii\) 从 \(WRI-1\) 递减到 \(-WIF\)）在真实值里权重为 \(2^{ii}\)，在码值里对应比特下标 `ii+WIF`。每试探把第 \(ii\) 位设为 1，相当于把候选根从 \(r\) 变成 \(r + 2^{ii}\)。

#### 4.2.2 核心流程

关键数学技巧：**不求新的平方，只求平方的增量**。把候选平方展开：

\[
(r + 2^{ii})^2 = r^2 + 2^{ii+1}\cdot r + 2^{2\cdot ii}
\]

所以候选的平方累计 = 当前累计 + 两项增量：

\[
\text{resu2}' = \text{resu2} + \underbrace{2^{ii+1}\cdot r}_{\text{项 A}} + \underbrace{2^{2\cdot ii}}_{\text{项 B}}
\]

把上式换算回**码值空间**（各项乘 \(2^{WIF}\)，注意 \(r\cdot 2^{WIF} = R\)）：

| 增量项 | 真实值 | 码值（× \(2^{WIF}\)） | 对应代码 |
| :--- | :--- | :--- | :--- |
| A | \(2^{ii+1}\cdot r\) | \(2^{ii+1}\cdot R\) = `resu<<(1+ii)` | `resu<<(1+ii)` 或右移 |
| B | \(2^{2\cdot ii}\) | \(2^{2\cdot ii+WIF}\) | `ONET<<(2*ii+WIF)` |

于是算法骨架（「试探 + 不够则恢复」）：

```
resu = 0,  resu2 = 0
for ii = WRI-1  down to  -WIF:
    候选 resu2tmp = resu2 + 项A + 项B       # = (把第ii位设为1后的平方累计)
    if resu2tmp <= inu  且  inu != 0:        # 够：候选根的平方没超过输入
        resu[ii+WIF] = 1                      # 保留这一位
        resu2 = resu2tmp                      # 接受新的累计
    # 否则：不动 resu 和 resu2 = 「恢复」
```

循环结束时 `resu2` 就是保留下的最大平方累计（\(\le\) `inu`），`resu` 就是对应的整数根码值，满足 \(R^2 \le I\cdot 2^{WIF}\) 且是「能放下的最大根」。

> 不变量：每轮开始时 `resu2 == resu²/2^WIF` 恒成立（初始 0=0² 成立；接受候选时两边同步更新）。这就是「逐位开方」得以正确收敛的数学保证。

#### 4.2.3 源码精读

上面表格里的「项 A / 项 B」对应循环体里的三行：

[RTL/fixedpoint.v:721-725](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L721-L725) —— `resu2tmp = resu2`（拷贝当前累计）；项 A：`ii>=0` 时 `resu2tmp += resu<<(1+ii)`，`ii<0` 时改成 `resu2tmp += resu>>(-1-ii)`；项 B：当 `2*ii+WIF>=0` 时 `resu2tmp += ONET<<(2*ii+WIF)`。

三个细节值得点出来：

- **项 A 的左移 / 右移分支**：当 `ii>=0`（根的整数位）时 \(2^{ii+1}\ge 1\)，用左移 `resu<<(1+ii)` 放大；当 `ii<0`（根的小数位）时 \(2^{ii+1}<1\)，需要把 `resu` **缩小**，于是改成右移 `resu>>(-1-ii)`。例如 `ii=-1` → 右移 0 位（即乘 \(2^0=1\)），`ii=-2` → 右移 1 位（即乘 \(2^{-1}\)）。右移天然带向下取整，丢弃的是低于 LSB 的小数部分。
- **项 B 的守卫 `2*ii+WIF>=0`**：当试探很低的小数位（\(2\cdot ii+WIF < 0\)）时，\(2^{2\cdot ii+WIF} < 1\)，在整数码值空间里这一项为 0。Verilog 不允许负的移位量，所以必须先用条件挡住，仅在移位量非负时才加。
- **`ONET` 与 `ONER` 的位宽**：`ONET` 是 `WTI+WIF` 位的 1，保证 `ONET<<(2*ii+WIF)` 不会因字面量太窄而被截断（和 `fxp_div` 用 `ONEA/ONEB` 同理）。

#### 4.2.4 代码实践

**目标**：用 Python 复现「逐位开方」的纯整数算法（与 RTL 无关，先建立直觉），验证它真的能算出平方根。

```python
# 示例代码：纯整数逐位开方（示意，非项目代码）
def fxp_sqrt_core(inu, WRI, WIF):
    resu = 0
    resu2 = 0
    ii = WRI - 1
    while ii >= -WIF:
        resu2tmp = resu2
        if ii >= 0:
            resu2tmp = resu2tmp + (resu << (1 + ii))      # 项A 左移
        else:
            resu2tmp = resu2tmp + (resu >> (-1 - ii))     # 项A 右移
        if 2*ii + WIF >= 0:
            resu2tmp = resu2tmp + (1 << (2*ii + WIF))     # 项B
        if resu2tmp <= inu and inu != 0:                  # 够：保留
            resu  = resu | (1 << (ii + WIF))
            resu2 = resu2tmp
        ii = ii - 1
    return resu, resu2
```

1. 取 `inu=10000, WRI=4, WIF=4`（即输入码 10000，格式近似 4 整数 4 小数），调用 `fxp_sqrt_core`，打印 `resu, resu2`。
2. 验证关系：`resu**2 / (1<<WIF)` 应约等于 `resu2`，且 `resu2 <= 10000`；真实根码 `resu` 对应 \(\sqrt{10000/2^4}=\sqrt{625}=25\)，即 `resu` 应为 25<<(相关) 量级。
3. **需要观察的现象**：`resu2 <= inu` 恒成立，且把 `resu` 当作 `(WRI,WIF)` 定点数还原后，其值接近 \(\sqrt{}\)` 的真实根。
4. **预期结果**：`resu` 与 `math.isqrt` 类语义一致（待本地验证；可对照 `resu**2` 与 `inu*(1<<WIF)` 的大小关系确认 `resu` 是「平方不超过输入的最大根」）。

#### 4.2.5 小练习与答案

**练习 1**：项 A 在 `ii<0` 时为什么用 `resu>>(-1-ii)` 而不是 `resu<<(1+ii)`？
**答案**：`ii<0` 时 `1+ii` 可能 \(\le 0\)，而 Verilog（以及本讲模型）的整数移位量必须非负。`-1-ii` 在 `ii<0` 时是非负的（如 `ii=-1→0`，`ii=-2→1`），且 `resu>>(-1-ii)` 在数值上正好等于 \(\lfloor resu \cdot 2^{1+ii}\rfloor\)，与左移 `1+ii` 位的数学含义一致。所以这是用「右移正数位」等价替换「左移负数位」。

**练习 2**：为什么判定里除了 `resu2tmp<=inu` 还要加 `inu!=0`？
**答案**：输入为 0 时 \(\sqrt{0}=0\)，根的所有位都应是 0。但若没有这个守卫，`inu=0` 时 `resu2tmp` 从 0 开始，`0 <= 0` 恒成立，会导致每一位都被错误地置 1，算出全 1 的根。`inu!=0` 守卫确保「输入为 0 → 根为 0」这一边界正确。

### 4.3 for 循环逐位试探的完整源码

#### 4.3.1 概念说明

4.2 节已经把数学和三行增量讲清楚了，本节把整个 `for` 循环作为一段完整源码集中精读，重点看「循环范围 → 根的哪些比特」「保留/恢复的写法」「`resu` 作为组合逻辑寄存器没有锁存器隐患」。

#### 4.3.2 核心流程

循环变量 `ii` 从 `WRI-1` 递减到 `-WIF`，正好覆盖根的每一个比特 `resu[ii+WIF]`：

- `ii = WRI-1` → 写 `resu[WRI-1+WIF]`，即根整数部分的最高位（权重 \(2^{WRI-1}\)）。
- `ii = 0` → 写 `resu[WIF]`，即根的个位（权重 \(2^0\)）。
- `ii = -WIF` → 写 `resu[0]`，即根的最低小数位（权重 \(2^{-WIF}\)）。

总共写 `WRI+WIF` 个比特 = `WRI` 位整数 + `WIF` 位小数，正好是根的精度。

#### 4.3.3 源码精读

[RTL/fixedpoint.v:721-730](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L721-L730) —— 逐位开方主循环。

逐行说明：

- `{resu2,resu} = 0;`（[第 720 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L720)）每次组合逻辑求值都把平方累计和部分根清零，从最高位重新搭起。这是 `always @ (*)`，不是时序逻辑，所以每个输入变化都完整重算一遍。
- `for(ii=WRI-1; ii>=-WIF; ii=ii-1)`：`ii` 是 `integer`（[第 711 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L711) 声明），可取负值，所以循环能同时覆盖整数位和小数位。
- `resu2tmp = resu2;` 然后累加项 A、项 B，得到「把第 `ii` 位设为 1 后的候选平方累计」。
- `if(resu2tmp<=inu && inu!=0) begin resu[ii+WIF]=1'b1; resu2=resu2tmp; end`：够（候选平方没超输入）就保留这一位、接受新累计；不够就什么都不做——`resu` 和 `resu2` 自动保持原值，这就是「恢复」语义。

注意 `resu` 虽是 `reg` 但工作在 `always @ (*)` 里，且每一位 `resu[ii+WIF]` 在循环中要么被置 1、要么保持上一轮的值（不会出现某位完全未赋值的情形，因为初始已清 0），所以综合时不会推断出锁存器，而是纯组合的「带保持的逐位写」。

> 关键路径：这个 `for` 循环会被综合成 `WRI+WIF` 级串联的「移位 + 加法 + 比较 + 选择」电路，每一位的 `resu2tmp` 都依赖前一位的 `resu2`，构成一条很长的组合链。这正是 README 标注「单周期版时序不易收敛」的根源，也是 [u3-l3 fxp_sqrt_pipe](./u3-l3-sqrt-pipe.md) 要把它展开成流水线的原因。

#### 4.3.4 代码实践

**目标**：通过 testbench 观察循环是否正确覆盖根的所有位，并用「完全平方输入」做零误差验证。

1. 复用 [SIM/tb_fxp_sqrt.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v) 的参数 `WII=10,WIF=10,WOI=6,WOF=12`。在该参数下，输入 20 位、根在 `(WRI+1=6, WIF=10)` 格式下为 16 位。
2. 在 testbench 的 `initial` 块（[第 79-127 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v#L79-L127)）里追加几条「完全平方」向量。由于输出根码 \(K\) 与输入码的关系是 `in_code = K²/2¹⁴`，可取：
   - `K=256` → `in_code=4` → `ival <= 20'd4;`（根应为 256，`oval1=1.0/16`）
   - `K=4096` → `in_code=1024` → `ival <= 20'd1024;`（根应为 4096，`oval1=1.0`）
   - `K=512` → `in_code=16` → `ival <= 20'd16;`（根应为 512，`oval1=1.0/8`）
3. 运行 `tb_fxp_sqrt_run_iverilog.bat`。
4. **需要观察的现象**：这几行打印里，`oval1^2`（testbench 第 71 行算的「根的平方」）应**精确等于**输入值 `ival`（无舍入误差），证明根是精确的。
5. **预期结果**：上述三组的 `oval1` 分别为 `0.0625`、`1.0`、`0.125`，且 `oval1^2` 与 `ival` 浮点值逐一吻合（待本地验证；单周期 `oval1` 与流水线 `oval2` 在对齐延迟后也应一致）。

#### 4.3.5 小练习与答案

**练习 1**：循环从 `ii=WRI-1` 递减到 `ii=-WIF`，共迭代多少次？为什么必须从高到低？
**答案**：共 `(WRI-1) - (-WIF) + 1 = WRI+WIF` 次。必须从高位开始，因为高位权重最大，先确定高位才能保证后续低位是在「扣除高位平方后的余量」上继续试探；若从低位开始，低位先填会立刻使平方累计爆掉，无法收敛到正确根。这与 `fxp_div` 必须从最高位商开始试探同理。

**练习 2**：`resu[ii+WIF]` 当 `ii=WRI-1` 时下标是多少？它代表根的哪一位？
**答案**：下标是 `WRI-1+WIF`，即根整数部分的最高位（真实权重 \(2^{WRI-1}\)）。例如 `WRI=5,WIF=10` 时下标为 14，是 16 位根里的第 15 个比特（最高整数位）。

### 4.4 结果还原：取补符号 + fxp_zoom 收敛位宽

#### 4.4.1 概念说明

经过 4.3 的循环，`resu` 是**无符号根的码值**（因为输入已取绝对值），它代表 \(\sqrt{|\text{in}|}\)。还差两步收尾：

1. **补符号**：若 `sign=1`（输入为负），把无符号根取补码负值，得到 \(-\sqrt{|\text{in}|}\)；若 `sign=0`，保持正根。这一步生成一个 `(WRI+1, WIF)` 格式的有符号量 `resushort`——多出的 1 位整数位用来放符号。
2. **位宽收敛**：用 `fxp_zoom` 把 `resushort` 从 `(WRI+1, WIF)` 搬到目标输出 `(WOI, WOF)`，顺带完成小数位舍入（`ROUND`）与整数位溢出饱和。**舍入和溢出检测全部集中在这个唯一的 `fxp_zoom` 里**，和 `fxp_mul` 一样是「一个 `fxp_zoom` 兜底」的两段式结构。

#### 4.4.2 核心流程

```
resushort = sign ? (~resu[WRI+WIF:0]) + ONER : resu[WRI+WIF:0]
# resushort 是 (WRI+1 整数, WIF 小数) 的有符号定点数

fxp_zoom:  (WRI+1, WIF)  --ROUND/溢出饱和-->  (WOI, WOF)
```

为什么送给 `fxp_zoom` 的整数位宽是 `WRI+1` 而不是 `WRI`？因为 `WRI` 位整数只能表示幅值，根还需要 1 位符号位才能表达正负，所以有符号根的整数位宽是 `WRI+1`。这也意味着：**只有当 `WOI < WRI+1` 时才可能发生整数位截断/溢出**；若 `WOI >= WRI+1`，`fxp_zoom` 只做符号扩展，永不溢出（小数位则按 `WOF` 与 `WIF` 的关系补零或截断舍入）。

#### 4.4.3 源码精读

取补符号这一行：

[RTL/fixedpoint.v:731](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L731) —— `resushort = sign ? (~resu[WRI+WIF:0])+ONER : resu[WRI+WIF:0];`

- `resu[WRI+WIF:0]` 取根的低 `WRI+WIF+1` 位（即 `WRI` 位整数 + `WIF` 位小数 + 1 位符号位），构成 `(WRI+1, WIF)` 格式。注意循环里实际只写了 `resu[0 .. WRI+WIF-1]`，第 `WRI+WIF` 位（最高位）保持初始的 0，正好充当正根的符号位 0。
- `sign ? (~resu[...])+ONER : resu[...]`：负数走 `(~x)+1` 取补码负值（即 \(-x\)），正数直接用。`ONER` 是 `WRI+WIF+1` 位的 1，保证加法位宽匹配。

随后唯一的 `fxp_zoom` 例化完成位宽收敛：

[RTL/fixedpoint.v:734-744](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L734-L744) —— `res_zoom` 把 `resushort` 从 `(WRI+1, WIF)` 搬到 `(WOI, WOF)`，`.ROUND(ROUND)` 让小数截断时按用户选择是否四舍五入，`overflow` 直接对外报告整数位溢出。

对比一下全库的「`fxp_zoom` 例化次数」：

- `fxp_add` / `fxp_addsub`：3 个（两路对齐 + 结果还原）。
- `fxp_mul` / `fxp_sqrt`：**1 个**（积/根的位宽天然确定，无需对齐，只做结果还原）。

`fxp_sqrt` 与 `fxp_mul` 同属「单 `fxp_zoom` 兜底」的精简结构——因为根的位宽 `WRI+1` 由输入 `WII` 唯一确定，不存在「两路输入要对齐」的问题。

#### 4.4.4 代码实践

**目标**：构造一个负数输入，验证模块的「取绝对值开方 + 补负号」符号处理。

1. 仍用 testbench 参数 `WII=10,WIF=10,WOI=6,WOF=12`。由 4.3.4 已知 `in_code=4`（正）对应根码 `K=256`。
2. 取负输入 `in_code = -4`，即 20 位补码 `'hFFFFC`。在 `initial` 块追加 `ival <= 20'hFFFFC;`。
3. 运行仿真。
4. **需要观察的现象**：该行打印的 `oval1` 应为 **负值** `-0.0625`（即 `-1/16`），且 `overflow1` 为 0；其幅值与正输入 `in_code=4` 时的根 `0.0625` 完全相同，只是加了负号。
5. **预期结果**：`oval1 = -0.0625`，`oval1^2 = 0.00390625`（与 `|in|=4/1024` 的平方根平方一致）。这印证了 `fxp_sqrt` 对负数的定义：\(\text{out} = -\sqrt{|\text{in}|}\)（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么送给 `fxp_zoom` 的整数位宽是 `WRI+1` 而不是 `WRI`？在什么配置下 `fxp_sqrt` 永不溢出？
**答案**：`WRI` 位只能表示根的幅值，还需 1 位符号位才能表达正负，故有符号根的整数位宽是 `WRI+1`。当 `WOI >= WRI+1` 时，`fxp_zoom` 在整数侧只做符号扩展、不截断，因此永不溢出（`overflow` 恒为 0）；testbench 用的 `WOI=6,WRI=5` 正好满足 `WOI = WRI+1`，所以那组测试不会触发整数溢出。

**练习 2**：`resushort` 的最高位（第 `WRI+WIF` 位）在循环里被写过吗？它在正/负输入下分别是什么？
**答案**：循环只写 `resu[0 .. WRI+WIF-1]`，第 `WRI+WIF` 位从未被循环赋值，保持声明时的初值 0。正输入（`sign=0`）时 `resushort` 直接取 `resu[WRI+WIF:0]`，最高位为 0，表示正数；负输入（`sign=1`）时走 `(~resu[...])+ONER`，取补码负值后最高位自然变 1，表示负数。所以这 1 位余量天然承担了符号位。

**练习 3**：如果用户把 `WOF` 设得比 `WIF` 小（输出小数位更少），`fxp_sqrt` 会出现什么行为？
**答案**：根的小数部分本来有 `WIF` 位（来自循环的 `ii=-1..-WIF`），但输出只保留 `WOF` 位，`fxp_zoom` 会截掉低 `WIF-WOF` 位小数。若 `ROUND=1`，按四舍五入处理这次截断（误差 \(\le \frac{1}{2}\text{LSB}\)）；若 `ROUND=0`，直接截断。整数位不受影响，溢出与否仍只看 `WOI` 与 `WRI+1` 的关系。

## 5. 综合实践

**任务**：基于 [SIM/tb_fxp_sqrt.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_sqrt.v) 搭建一个自校验环境，分三类用例验证 `fxp_sqrt`：完全平方（零误差）、负数（符号处理）、随机输入（与软件 `sqrt()` 参考比误差）。

操作步骤：

1. **完全平方用例**：在 `initial` 块追加 4.3.4 推导的向量 `in_code = 4, 16, 1024`（对应根码 256/512/4096），它们在 `WII=10,WIF=10,WOI=6,WOF=12` 下都是精确平方根。**判定标准**：`oval1^2` 与输入浮点值严格相等（误差为 0）。
2. **负数用例**：追加 `ival <= 20'hFFFFC;`（即 `in_code=-4`）。**判定标准**：`oval1 = -0.0625`，`overflow1=0`，幅值与正输入 `in_code=4` 一致。
3. **随机用例**：用 Python 生成若干随机 20 位补码码值，对每个码值算软件参考 `ref = (sign?-1:1) * sqrt(|$signed(code)|/2**WIF)`；把同一批码值作为 `ival` 灌进 testbench（可在 `initial` 块里逐拍赋值，或用 `$readmemh` 从文件读入）。**判定标准**：每个随机输入下 `oval1` 与 `ref` 的误差 \(\le 1\text{ LSB} = 2^{-WOF} = 2^{-12}\)（除非发生溢出，此时 `overflow1=1` 且 `oval1` 饱和）。
4. 运行 `tb_fxp_sqrt_run_iverilog.bat`，人工或脚本核对三类判定。
5. **需要观察的现象**：完全平方组误差恒为 0；负数组幅值正确、符号为负；随机组误差在 1 LSB 内或正确报溢出。
6. **预期结果**：三类用例全部通过；其中随机组若 `WOI < WRI+1`（例如把 `WOI` 改成 4）则会观察到部分大根触发 `overflow1=1` 并饱和到正最大，可用于额外验证溢出通路（待本地验证）。

> 提示：testbench 第 67-75 行已经把 `ival`、`oval1`、`oval1^2`、`overflow1` 都打印出来了，你可以直接对照这几列做判定，不必额外加 `$display`。注意 `oval2` 是流水线版本 `fxp_sqrt_pipe` 的输出，它要等 `[WII/2]+WIF+2` 拍延迟后才有效——本讲只验证单周期 `oval1`，流水线对齐留到 [u3-l3](./u3-l3-sqrt-pipe.md)。

## 6. 本讲小结

- `fxp_sqrt` 是单周期（纯组合逻辑）定点开方，README 标注「时序不易收敛」，工程中应优先用 [u3-l3 fxp_sqrt_pipe](./u3-l3-sqrt-pipe.md)。
- 结果整数位宽取 `WRI = WTI/2`，其中 `WTI` 是把输入整数位宽 `WII` 凑成偶数后的值——体现「平方根位数约为原数一半」。
- 它先把输入取绝对值得到 `inu`、记下符号 `sign`，把有符号开方归约成无符号开方；对负数定义为 \(-\sqrt{|\text{in}|}\)。
- 核心 `for(ii=WRI-1; ii>=-WIF; ...)` 是逐位开方：维护部分根 `resu` 与平方累计 `resu2`（不变量 `resu2 == resu²/2^WIF`），每试探一位用平方展开增量 \((resu+2^{ii})^2 - resu^2 = 2^{ii+1}resu + 2^{2ii}\) 算出候选 `resu2tmp`，`resu2tmp ≤ inu`（且 `inu!=0`）则保留该位、否则恢复。
- 增量的两项对应代码 `resu<<(1+ii)`（或 `ii<0` 时的 `resu>>(-1-ii)`）与 `ONET<<(2*ii+WIF)`，后者用 `2*ii+WIF>=0` 守卫挡住非法的负移位。
- 收尾按 `sign` 用 `(~resu)+ONER` 把无符号根补成有符号的 `resushort`，再用唯一的 `fxp_zoom` 从 `(WRI+1, WIF)` 收敛到 `(WOI, WOF)` 并兜底舍入与溢出饱和——与 `fxp_mul` 同属「单 `fxp_zoom`」精简结构。

## 7. 下一步学习建议

- **流水线化**：本模块的关键路径是 `WRI+WIF` 级串联的「移位 + 加法 + 比较」，正是「时序不易收敛」的根源。下一讲 [u3-l3 fxp_sqrt_pipe](./u3-l3-sqrt-pipe.md) 会把这个 `for` 循环展开成 `[WII/2]+WIF+2` 级流水线，用 `sign/inu/resu/resu2` 数组做级间寄存器，建议对照本讲的循环体阅读，体会「循环变量 `ii` → 流水线级 `jj=WRI-1-ii`」的映射。
- **对比除法**：[u2-l3 fxp_div](./u2-l3-div.md) 的恢复余数法与本讲的逐位开方是同类「试探 + 不够则恢复」算法，对照阅读（一个维护 `商×除数`，一个维护 `根²`）能加深对 digit-recurrence 类硬件算法的整体把握。
- **深入 fxp_zoom**：本讲的舍入与溢出饱和全部由结尾那个 `fxp_zoom` 完成，若对其 `ROUND` 舍入、上下溢出饱和的细节还不够熟，回头重读 [u1-l3](./u1-l3-fxp-zoom.md) 的第 41–94 行。
- **仿真方法学**：本综合实践的「完全平方零误差 + 软件参考比误差」套路，在 [u3-l6 仿真验证方法学](./u3-l6-simulation-testbench.md) 会有系统总结，建议届时把这里的随机比对改写成 testbench 内带 pass/fail 计数器的自校验逻辑。
