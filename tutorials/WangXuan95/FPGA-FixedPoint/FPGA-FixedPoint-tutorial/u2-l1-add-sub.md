# fxp_add 与 fxp_addsub：基于 fxp_zoom 的加减法

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `fxp_add` 与 `fxp_addsub` 共同采用的**「对齐 → 运算 → 还原」三段式结构**，并解释为什么加减法要反复借助 `fxp_zoom`。
- 手算出 `fxp_add` 中公共位宽 `WII/WIF`、结果中间位宽 `WRI=WII+1` 是怎么来的，以及为什么这一步多留 1 位整数位。
- 读懂 `fxp_addsub` 如何用 `sub` 控制位、借助**补码取反** `(~inbe)+ONE` 把减法统一成加法。
- 解释 `fxp_add` 与 `fxp_addsub` 在中间位宽计算上的关键差异：为什么 `addsub` 要先把 `inb` 扩展 1 位整数（`WIIBE=WIIB+1`）。
- 自己写一个 testbench，同时验证 `fxp_add` 与 `fxp_addsub(sub=0)` 功能等价，并能构造出上溢出/下溢出的用例。

## 2. 前置知识

本讲默认你已经学过 **u1-l2（定点数格式与统一参数命名）** 与 **u1-l3（fxp_zoom 位宽变换核心）**。这里只做最简回顾：

- **定点数值** = 把二进制码当成**有符号补码整数**再除以 \(2^{W_F}\)。解码用 \(v = c / 2^{W_F}\)，仿真里的万能写法是 `$signed(code)*1.0/(1<<W)`。
- **整数位宽 \(W_I\)（含 1 位符号位）** 决定表示范围 \([-2^{W_I-1},\ 2^{W_I-1}-2^{-W_F}]\)；**小数位宽 \(W_F\)** 决定精度 \(2^{-W_F}\)。
- **`fxp_zoom`** 把定点数从格式 \((W_{II},W_{IF})\) 搬到 \((W_{OI},W_{OF})\)：小数位先对齐（截断时按 `ROUND` 四舍五入），整数位再对齐（\(W_{OI}<W_{II}\) 时检测上溢出/下溢出并**饱和钳位**）。
- 全库统一参数命名：`WOI/WOF`（输出）、`WIIA/WIFA`、`WIIB/WIFB`（双目输入 A/B），端口位宽写作 `WII+WIF`、`WOI+WOF`。

一个贯穿本讲的关键直觉：**两个不同格式（不同整数位宽、不同小数位宽）的定点数不能直接相加**，必须先搬到同一个「公共格式」上对齐小数点，才能做有符号加法；算完之后，再搬到目标输出格式，并在这一步处理舍入与溢出。这件「搬运」的活，全部由 `fxp_zoom` 完成。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [RTL/fixedpoint.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v) | 全部可综合模块所在的单文件。本讲读其中的 `fxp_zoom`（L14–L94）、`fxp_add`（L102–L170）、`fxp_addsub`（L178–L262）。 |
| [SIM/tb_add_sub_mul_div.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v) | 加减乘除的仿真测试平台。本讲关注它如何例化 `fxp_add`/`fxp_addsub`、以及用 `$signed` 还原浮点做软件参考的套路。 |

## 4. 核心概念与源码讲解

### 4.1 fxp_zoom 在加减法中扮演的「位宽搬运工」角色

#### 4.1.1 概念说明

`fxp_zoom` 本身的内部细节（截断舍入、上下溢出饱和）已在 u1-l3 讲透，本讲**不重复**它的实现，而是聚焦它**在加减法里被如何调用**——这是理解 `fxp_add`/`fxp_addsub` 的钥匙。

加减法里会出现**三种用途**的 `fxp_zoom`：

1. **输入对齐（ina_zoom / inb_zoom）**：把 A、B 两路输入各自搬到公共格式 \((W_{II},W_{IF})\)，让它们小数点对齐、整数位宽统一，才能相加。这一步**只做扩展、不做截断**，所以一定设 `.ROUND(0)`，且绝不会溢出。
2. **被减数扩展（inb_extend，仅 addsub 有）**：先把 `inb` 的整数位宽 +1，为后面的补码取反留出符号余量。
3. **结果还原（res_zoom）**：把加完的中间结果从 \((W_{RI},W_{RF})\) 搬回输出格式 \((W_{OI},W_{OF})\)。这一步**才会发生截断与溢出**，所以它带 `.ROUND(ROUND)`，并且把 `overflow` 输出接出来。

也就是说，加减法的**舍入与溢出检测全部集中在最后那个 `res_zoom`**，前面两个输入侧的 `fxp_zoom` 只负责「无副作用地把位宽对齐」。这是全库统一的设计模式（乘除开方也都遵循）。

#### 4.1.2 核心流程

```text
ina (WIIA,WIFA) ──┐
                  ├─→ [ina_zoom, ROUND=0] ──→ inaz (WII,WIF) ──┐
inb (WIIB,WIFB) ──┘                                              ├─→ $signed(+) ─→ res (WRI,WRF)
                  ──→ [inb_zoom, ROUND=0] ──→ inbz (WII,WIF) ──┘                       │
                                                                                    ▼
                                                              [res_zoom, ROUND=ROUND] ─→ out (WOI,WOF), overflow
```

#### 4.1.3 源码精读

`fxp_zoom` 的整数位溢出饱和逻辑（加减法最终溢出就在这里被判出）位于：

[RTL/fixedpoint.v:65-90](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L65-L90) —— 当 \(W_{OI}<W_{II}\) 时：正超限 → `overflow=1` 且 `out` 钳位到正最大；负超限 → `overflow=1` 且 `out` 钳位到负最小。这段就是加减法「加爆了」之后兜底的代码。

小数侧的 `ROUND` 四舍五入实现在：

[RTL/fixedpoint.v:41-54](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L41-L54) —— 仅当输出小数位宽更窄（\(W_{OF}<W_{IF}\)）时才生效；这也是为什么输入侧 `ROUND=0` 完全无所谓（输入侧只扩展不截断，根本走不到这里）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用肉眼数清 `fxp_add` 和 `fxp_addsub` 各例化了几个 `fxp_zoom`，并标注每一个的 `.ROUND` 取值。

**步骤**：

1. 打开 [RTL/fixedpoint.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v)，定位 `fxp_add`（L110 起）与 `fxp_addsub`（L186 起）。
2. 数 `fxp_zoom` 例化点：`fxp_add` 有 3 个（`ina_zoom`、`inb_zoom`、`res_zoom`），`fxp_addsub` 有 4 个（多了 `inb_extend`）。
3. 检查每个的 `.ROUND`：输入侧全部是 `0`，结果侧是模块自己的 `ROUND` 参数。

**预期结果**：你会发现「输入侧 `ROUND=0`、结果侧 `ROUND=ROUND`」是铁律，这印证了 4.1.1 的结论——只有最后一步才可能舍入与溢出。

#### 4.1.5 小练习与答案

**Q1**：为什么输入侧的 `ina_zoom`/`inb_zoom` 用 `.ROUND(0)`，却不会损失精度？

**答**：因为公共小数位宽 \(W_{IF}=\max(W_{IFA},W_{IFB})\) 不小于任一输入的小数位宽，输入侧只做**小数位扩展（左移补零）**，从不截断，所以 `ROUND` 取 0 或 1 都不会触发，自然无损。

**Q2**：如果把结果侧 `res_zoom` 的 `.ROUND(ROUND)` 改成 `.ROUND(0)`，加减法还能用吗？

**答**：能用，但失去四舍五入、变成直接截断，最大误差从 \(½\text{LSB}\) 增大到 \(1\text{LSB}\)。溢出检测不受影响（它在整数侧）。

---

### 4.2 fxp_add：公共位宽对齐 + 加法 + 还原溢出

#### 4.2.1 概念说明

`fxp_add` 是纯加法模块。它要解决的核心问题是：A、B 两个输入可能有**不同的整数位宽和小数位宽**，怎么把它们加起来？

答案是先各自搬到**公共格式**，再相加。公共格式取「两者里更宽的那个」：

- 公共整数位宽 \(W_{II}=\max(W_{IIA},W_{IIB})\)
- 公共小数位宽 \(W_{IF}=\max(W_{IFA},W_{IFB})\)

搬过去之后，两路数据小数点对齐、符号位对齐，就能做普通的有符号加法了。

#### 4.2.2 核心流程

先看结果位宽为什么要多留 1 位。两个范围在 \([-2^{W_{II}-1},\ 2^{W_{II}-1}-2^{-W_{IF}}]\) 内的数相加，和的范围是：

\[
-2^{W_{II}} \;\le\; A+B \;\le\; 2^{W_{II}} - 2^{-W_{IF}+1}
\]

也就是说，和的整数部分可能用到第 \(W_{II}\) 位（最高位进位），比单个输入多 1 位整数。所以**中间结果的整数位宽**取：

\[
W_{RI} = W_{II} + 1,\qquad W_{RF}=W_{IF}
\]

这一步多留的 1 位，**保证加法本身永远不会溢出**——真正的「是否超出输出范围」的判断，推迟到最后一步 `res_zoom` 根据 \(W_{OI}\) 来做。

完整流程：

```text
1. WII  = max(WIIA, WIIB)        // 公共整数位宽
   WIF  = max(WIFA, WIFB)        // 公共小数位宽
   WRI  = WII + 1                // 结果整数位宽，多 1 位吸收进位
2. inaz = zoom(ina, (WIIA,WIFA) → (WII,WIF), ROUND=0)
   inbz = zoom(inb, (WIIB,WIFB) → (WII,WIF), ROUND=0)
3. res  = $signed(inaz) + $signed(inbz)        // 宽 WRI+WRF 的有符号加法
4. {out, overflow} = zoom(res, (WRI,WRF) → (WOI,WOF), ROUND=ROUND)
```

#### 4.2.3 源码精读

模块端口与参数，注意双目输入各自带 A/B 后缀：

[RTL/fixedpoint.v:110-123](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L110-L123) —— `fxp_add` 的 parameter 列表（`WIIA/WIFA/WIIB/WIFB/WOI/WOF/ROUND`）与端口（`ina`、`inb`、`out`、`overflow`）。

公共位宽与中间结果位宽的定义：

[RTL/fixedpoint.v:125-128](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L125-L128) —— 用三目运算取 `max` 得到 `WII`/`WIF`，再令 `WRI=WII+1`、`WRF=WIF`。这正是 4.2.2 里那组公式的直接翻译。

中间加法（连续赋值写在 wire 声明里）：

[RTL/fixedpoint.v:130-132](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L130-L132) —— `inaz`/`inbz` 是无符号 wire，用 `$signed()` 转成有符号再相加，结果 `res` 宽 `WRI+WRF` 位、带 `signed`。`$signed` 是关键：没有它，Verilog 会按无符号算，负数就全错了。

三个 `fxp_zoom` 例化：

[RTL/fixedpoint.v:134-168](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L134-L168) —— `ina_zoom`/`inb_zoom` 把两路输入搬到 \((W_{II},W_{IF})\) 且 `.ROUND(0)`、`overflow` 口悬空不接；`res_zoom` 把 `res` 搬到 \((W_{OI},W_{OF})\) 且 `.ROUND(ROUND)`，并把 `overflow` 接到模块输出。注意 `res` 是 `signed`，传给 `fxp_zoom` 时用 `$unsigned(res)` 转回无符号位向量（`fxp_zoom` 内部自己会再 `$signed`）。

#### 4.2.4 代码实践（跟踪型）

**目标**：给定一组位宽配置，手算 `res` 的位宽，并验证它足以容纳「两路正最大值相加」。

**步骤**：

1. 取配置 `WIIA=WIFA=WIIB=WIFB=8`（即 8 整数 8 小数）。则 `WII=max(8,8)=8`，`WIF=8`，`WRI=WII+1=9`，`WRF=8`。
2. 两路正最大值都是 \(127.99609375\)（码 `0x7FFF`），相加得 \(\approx 255.992\)，对应码 \(\approx 65534\)。
3. `res` 宽 `WRI+WRF = 9+8 = 17` 位有符号，能表示到 \(2^8-2^{-8}\approx 255.996\)，恰好够装下 \(65534\)，**加法本身不溢出**。
4. 如果此时 `WOI=8`，则 `res_zoom` 会发现 \(W_{OI}=8 < W_{RI}=9\)，判定**上溢出**，把 `out` 钳到正最大 \(127.99609375\) 并置 `overflow=1`。

**预期结果**：「加法在 `res` 里永远不溢出，溢出只发生在 `res_zoom` 对照 `WOI` 时」——这就是 `WRI=WII+1` 设计的意义。

> 待本地验证：第 4 步的溢出结论可用 4.4 节的 testbench 实跑确认。

#### 4.2.5 小练习与答案

**Q1**：`fxp_add` 里 `WRF` 为什么直接等于 `WIF`，而不是 `WIF+1`？

**答**：加法不会让小数位数变多（两个 \(W_{IF}\) 位小数的和仍是 \(W_{IF}\) 位小数，最多整数位进位），所以小数位宽不变；只有整数位需要 +1 吸收进位。

**Q2**：若 `WOI >= WII+1`（输出整数位宽足够大），`fxp_add` 还可能产生 `overflow=1` 吗？

**答**：不可能。此时 `res_zoom` 里 \(W_{OI} \ge W_{RI}=W_{II}+1\)，走的是「只做符号扩展、不检测溢出」的分支，`overflow` 恒为 0。这是判断一套加法配置是否会溢出的快捷方法。

---

### 4.3 fxp_addsub：sub 控制位与补码减法

#### 4.3.1 概念说明

`fxp_addsub` 比 `fxp_add` 多了一个 1 位输入 `sub`：`sub=0` 做加法、`sub=1` 做减法（`a-b`）。它**不单独写一套减法电路**，而是利用补码的性质——

\[
a - b = a + (-b)
\]

在二进制补码里，\(-b = \sim b + 1\)（按位取反再加 1）。所以只要在 `inb` 这一路上，根据 `sub` 选择「送 \(b\)」还是「送 \(\sim b+1\)」，后面就可以复用同一套加法逻辑。

但这里藏着一个陷阱：**补码取反可能让位数变多**。比如 `inb` 是 8 位整数时，最负值 \(-128\) 取反得 \(+128\)，已经装不回 8 位整数（8 位有符号最大是 \(+127\)）。所以 `addsub` 在取反之前，先**把 `inb` 的整数位宽扩展 1 位**（`WIIBE = WIIB+1`），让取反永远安全。

#### 4.3.2 核心流程

```text
WIIBE = WIIB + 1                              // 给 inb 多留 1 位整数，供取反使用
WII   = max(WIIA, WIIBE)                       // 公共整数位宽（注意是 WIIBE，不是 WIIB）
WIF   = max(WIFA, WIFB)
WRI   = WII + 1

inbe  = zoom(inb, (WIIB,WIFB) → (WIIBE,WIFB), ROUND=0)   // 仅扩展整数位
inbv  = sub ? (~inbe) + 1 : inbe              // sub=1 → 取反得 -b；sub=0 → 原值 b
inaz  = zoom(ina, (WIIA,WIFA) → (WII,WIF),  ROUND=0)
inbz  = zoom(inbv,(WIIBE,WIFB)→ (WII,WIF),  ROUND=0)
res   = $signed(inaz) + $signed(inbz)         // a + b  或  a + (-b) = a - b
{out,overflow} = zoom(res, (WRI,WRF) → (WOI,WOF), ROUND=ROUND)
```

可见 `sub` 只在生成 `inbv` 这一处起作用，其余与 `fxp_add` 完全同构。

#### 4.3.3 源码精读

端口比 `fxp_add` 多了 `sub`：

[RTL/fixedpoint.v:186-200](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L186-L200) —— 注意注释 `// 0=add, 1=sub` 标明了 `sub` 的含义。

位宽参数：先定义 `WIIBE=WIIB+1`，再用它参与 `max`：

[RTL/fixedpoint.v:202-208](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L202-L208) —— `WII = WIIA>WIIBE ? WIIA : WIIBE`，这是 `addsub` 与 `add` 最核心的差异点（`add` 里是 `max(WIIA,WIIB)`）。

补码取反的选择逻辑：

[RTL/fixedpoint.v:209-212](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L209-L212) —— `inbv = sub ? (~inbe)+ONE : inbe`，其中 `ONE` 是宽 `WIIBE+WIFB` 的常量 1（位于 [RTL/fixedpoint.v:208](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L208)）。`(~inbe)+ONE` 正是补码取反 \(-b\)。

四个 `fxp_zoom` 例化：

[RTL/fixedpoint.v:214-260](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L214-L260) —— `inb_extend`（把 `inb` 扩到 `WIIBE` 位整数）、`ina_zoom`、`inb_zoom`（搬 `inbv` 到公共格式）、`res_zoom`（还原输出并检测溢出）。比 `fxp_add` 多出来的就是 `inb_extend`。

#### 4.3.4 代码实践（验证型）

**目标**：确认 `sub=1` 时 `fxp_addsub` 真的等于「`ina - inb`」。

**步骤**：

1. 复用本讲 4.4 综合实践的 testbench，把 `fxp_addsub` 的 `.sub(1'b0)` 改成 `.sub(1'b1)`。
2. 在比对逻辑里，把软件参考从 `ina+inb` 改成 `ina-inb`（仿照 [SIM/tb_add_sub_mul_div.v:109-115](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L109-L115) 里现成的减法打印）。
3. 用若干组随机输入跑一遍。

**预期结果**：`fxp_addsub` 的输出与 `$signed(ina)*1.0/(1<<WIFA) - $signed(inb)*1.0/(1<<WIFB)` 的差值在 \(1\text{LSB}\) 以内（受 `res_zoom` 舍入影响）。

> 待本地验证：随机种子的具体数值每次不同，但误差应稳定在 1 LSB 内。

#### 4.3.5 小练习与答案

**Q1**：`(~inbe)+ONE` 里的 `ONE` 为什么必须声明成 `WIIBE+WIFB` 位宽，而不是直接写整数 `1`？

**答**：补码取反需要「在最低位（\(2^{-W_{IFB}}\) 那一位）加 1」。如果直接写无位宽的 `1`，Verilog 可能把它当成 32 位整数的 1，加到高位去就错了。声明成 `WIIBE+WIFB` 位的 1，保证这个 1 落在小数最低位，完成正确的 \(+1\)。

**Q2**：`sub=0` 时，`fxp_addsub` 多走了 `inb_extend` 这一步，结果会和 `fxp_add` 不同吗？

**答**：数值上完全相同。`inb_extend` 只是给 `inb` 多加 1 位符号位（值不变），后面 `inbv=inbe`、再加法，多出来的位只是符号扩展；最终 `res_zoom` 会把它们一并收敛到 \((W_{OI},W_{OF})\)。这就是 4.4 节要验证的「等价性」。

---

### 4.4 对比 add 与 addsub：为什么中间位宽 WRI 会相差 1 位

#### 4.4.1 概念说明

把 4.2 和 4.3 放在一起对比，最值得记住的一点是**中间整数位宽 `WII`（因而 `WRI=WII+1`）的取法不同**：

| 模块 | 公共整数位宽 `WII` | 结果整数位宽 `WRI` | `fxp_zoom` 个数 |
| --- | --- | --- | --- |
| `fxp_add` | \(\max(W_{IIA}, W_{IIB})\) | \(\max(W_{IIA}, W_{IIB})+1\) | 3 |
| `fxp_addsub` | \(\max(W_{IIA}, W_{IIB}+1)\) | \(\max(W_{IIA}, W_{IIB}+1)+1\) | 4 |

差异的根源：`addsub` 要在 `inb` 路上做补码取反 \(\sim b+1\)，而**最负值取反会多出 1 位**，所以必须先把 `inb` 扩到 `WIIB+1` 位整数，`WII` 因此可能比 `add` 大 1。`fxp_add` 不取反，就没有这 1 位的开销。

#### 4.4.2 核心流程（等价性证明思路）

当 `sub=0` 时，`fxp_addsub` 走的是 `inbv=inbe`（即扩展后的 `inb`，值不变），整条数据通路相比 `fxp_add` 只是**多了若干位符号扩展**。由于 `fxp_zoom` 在 `W_{OI}\ge W_{II}` 时只做符号扩展、不改变数值，两个模块最终都会把同一个数学和 \(A+B\) 收敛到同一个 \((W_{OI},W_{OF})\) 输出，于是：

- `out` 完全相同；
- `overflow` 完全相同（都由同一个 `res_zoom` 的整数侧判定）。

这就是综合实践里「`fxp_add` 与 `fxp_addsub(sub=0)` 必然一致」的理论依据。

#### 4.4.3 源码精读（对照两段）

`fxp_add` 的 `WII` 定义：

[RTL/fixedpoint.v:125-127](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L125-L127) —— `WII = WIIA>WIIB ? WIIA : WIIB`。

`fxp_addsub` 的 `WII` 定义：

[RTL/fixedpoint.v:202-206](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L202-L206) —— 先 `WIIBE=WIIB+1`，再 `WII = WIIA>WIIBE ? WIIA : WIIBE`。两段并排看，差异一目了然。

仿真侧的对照参考：官方 testbench 同时例化了两者（`fxp_add` 做加、`fxp_addsub` 固定 `sub=1'b1` 做减），见：

[SIM/tb_add_sub_mul_div.v:29-59](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L29-L59) —— 同一组 `ina/inb` 同时喂给 `fxp_add_i` 与 `fxp_addsub_i`，输出分别是 `oadd`（加）和 `osub`（减）。

#### 4.4.4 代码实践（源码阅读型）

**目标**：用具体数字感受「最负值取反需要多 1 位」。

**步骤**：

1. 设 `WIIB=8`，则 `inb` 最负值是 `-128`（码 `0x80`）。
2. 想象**不**做 `inb_extend` 直接取反：`~0x80 + 1` 在 8 位里 = `0x80`（还是 `-128`），出错了——因为 \(+128\) 装不进 8 位有符号。
3. 现在先扩展到 9 位：`inb` = `0x180`（符号扩展，值仍 `-128`），再 `~0x180+1` = `0x080` = `+128`，正确。
4. 回到源码 [RTL/fixedpoint.v:214-224](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L214-L224)，确认 `inb_extend` 正是把整数位从 `WIIB` 扩到 `WIIBE=WIIB+1`。

**预期结果**：你会直观理解「为什么 `addsub` 的 `WII` 要用 `WIIB+1` 而不是 `WIIB`」——补码取反的边界安全全靠这一步。

#### 4.4.5 小练习与答案

**Q1**：若 `WIIA` 本来就比 `WIIB` 大很多（比如 `WIIA=16, WIIB=8`），`addsub` 的 `WII` 和 `add` 的 `WII` 还会不同吗？

**答**：不会。此时 `add` 的 `WII=max(16,8)=16`；`addsub` 的 `WII=max(16, 8+1)=max(16,9)=16`，两者都是 16。差异只在 `WIIB >= WIIA` 时才显现（多出 1 位）。

**Q2**：能不能把 `fxp_add` 完全当成 `fxp_addsub` 用（把减法也通过它实现）？

**答**：不能直接。`fxp_add` 没有 `sub` 口，也没有 `inb_extend` 那条取反通路。要做减法，要么用 `fxp_addsub(sub=1)`，要么自己在调用 `fxp_add` 前对 `inb` 做补码取反——但自己取反时要注意位数是否够（即上面 Q1 讨论的边界问题），所以库提供的 `fxp_addsub` 才是稳妥之选。

---

## 5. 综合实践

把本讲的三件事——**`fxp_add` 与 `fxp_addsub(sub=0)` 等价**、**上溢出饱和到正最大**、**下溢出饱和到负最小**——用一个自校验 testbench 一次跑通。

为方便触发溢出，选用 `WIIA=WIFA=WIIB=WIFB=WOI=WOF=8` 的配置：此时 `WRI=9 > WOI=8`，加法**可能**溢出。

下面的代码是**示例代码**（请读者存为 `SIM/tb_add_eq_addsub.v`，并仿照官方 [tb_add_sub_mul_div_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div_run_iverilog.bat) 写一个 `.bat`，把两个源文件一起编译）：

```verilog
`timescale 1ps/1ps
module tb_add_eq_addsub ();
    localparam WIIA=8, WIFA=8, WIIB=8, WIFB=8, WOI=8, WOF=8;

    reg  [WIIA+WIFA-1:0] ina = 0;
    reg  [WIIB+WIFB-1:0] inb = 0;
    wire [WOI +WOF -1:0] oadd, osub;
    wire                 oaddo, osubo;

    fxp_add #(.WIIA(WIIA),.WIFA(WIFA),.WIIB(WIIB),.WIFB(WIFB),
              .WOI(WOI),.WOF(WOF),.ROUND(1))
    u_add (.ina(ina),.inb(inb),.out(oadd),.overflow(oaddo));

    // sub=0：addsub 退化为加法，应与 fxp_add 完全一致
    fxp_addsub #(.WIIA(WIIA),.WIFA(WIFA),.WIIB(WIIB),.WIFB(WIFB),
                 .WOI(WOI),.WOF(WOF),.ROUND(1))
    u_addsub (.ina(ina),.inb(inb),.sub(1'b0),.out(osub),.overflow(osubo));

    integer i, mism;
    initial begin
        mism = 0;
        // (1) 随机一致性：add 与 addsub(sub=0) 必须逐位一致
        for (i=0; i<200; i=i+1) begin
            ina = $random; inb = $random; #1000;
            if (oadd !== osub || oaddo !== osubo) begin
                mism = mism + 1;
                $display("MISMATCH ina=%h inb=%h add=%h/%b sub0=%h/%b",
                         ina, inb, oadd, oaddo, osub, osubo);
            end
        end
        // (2) 上溢出：正最大 + 正最大 → 饱和到正最大，overflow=1
        ina = {1'b0, {(WIIA+WIFA-1){1'b1}}};   // +127.99609375
        inb = {1'b0, {(WIIB+WIFB-1){1'b1}}};   // +127.99609375
        #1000;
        $display("posmax+posmax : HW=%f overflow=%b (期望 HW=127.99609375, overflow=1)",
                 $signed(oadd)*1.0/(1<<WOF), oaddo);
        // (3) 下溢出：负最小 + 负最小 → 饱和到负最小，overflow=1
        ina = {1'b1, {(WIIA+WIFA-1){1'b0}}};   // -128.0
        inb = {1'b1, {(WIIB+WIFB-1){1'b0}}};   // -128.0
        #1000;
        $display("negmin+negmin  : HW=%f overflow=%b (期望 HW=-128.000000, overflow=1)",
                 $signed(oadd)*1.0/(1<<WOF), oaddo);

        $display("=== 一致性比对结束，MISMATCH 次数 = %0d (期望 0) ===", mism);
        $finish;
    end
endmodule
```

**操作步骤**：

1. 在 `SIM/` 下新建上面的 `tb_add_eq_addsub.v`。
2. 仿照官方脚本写一行编译命令：`iverilog -g2001 -o sim.out tb_add_eq_addsub.v ../RTL/fixedpoint.v`，再 `vvp -n sim.out`。
3. 阅读终端输出。

**需要观察的现象与预期结果**：

- 随机循环段**不应**打印任何 `MISMATCH`，末行 `MISMATCH 次数 = 0`——证明 `fxp_add` 与 `fxp_addsub(sub=0)` 完全等价（对应 4.4.2 的结论）。
- `posmax+posmax` 行：`HW=127.99609375`、`overflow=1`——上溢出饱和到正最大。
- `negmin+negmin` 行：`HW=-128.000000`、`overflow=1`——下溢出饱和到负最小。

> 待本地验证：`$random` 每次种子不同，但「无 MISMATCH」与两个溢出用例的饱和值是确定的，必然复现。

## 6. 本讲小结

- `fxp_add` 与 `fxp_addsub` 都采用**「输入对齐 → 有符号加法 → 结果还原」三段式**，舍入与溢出检测**只集中在最后那个 `res_zoom`**。
- 公共位宽取 `WII=max(WIIA,WIIB)`、`WIF=max(WIFA,WIFB)`；结果中间位宽 `WRI=WII+1`，多出的 1 位整数用来吸收加法进位，保证加法本身不溢出。
- `fxp_addsub` 用 `sub` 控制位、靠补码取反 `(~inbe)+ONE` 把减法统一成加法；多出的 `inb_extend` 先把 `inb` 扩到 `WIIB+1` 位整数，确保最负值取反不越界。
- 因此 `addsub` 的 `WII=max(WIIA,WIIB+1)` 比 `add` 的 `WII=max(WIIA,WIIB)` 可能大 1——这是两个模块中间位宽差异的唯一根源。
- `sub=0` 时 `fxp_addsub` 与 `fxp_add` 数值完全等价（多余位都是符号扩展），可用 testbench 逐位验证。
- 是否会溢出只取决于输出位宽：当 `WOI >= WII+1` 时加法永不溢出；`WOI` 更窄时，超出范围会被 `res_zoom` 饱和钳位并置 `overflow=1`。

## 7. 下一步学习建议

- 顺着「结果位宽 = 输入位宽之和」的思路，下一讲 **u2-l2（fxp_mul 乘法与结果位宽推导）** 会展示乘积位宽为何是 `WRI=WIIA+WIIB`、`WRF=WIFA+WIFB`，并把全精度积交给同一个 `fxp_zoom` 收敛——你会看到加减法与乘法在「最后一步都靠 `res_zoom`」上的高度一致。
- 若想看「被加数/被减数需要更宽中间格式」的另一个例子，可继续阅读 **u2-l3（fxp_div）**，它的 `WRI/WRF` 取法（`max(WOI+WIIB, WIIA)` 等）是同一种「先把操作数搬到足够宽的公共格式」思想在除法上的推广。
- 在动手下一讲前，建议先把本讲综合实践的 testbench 跑通，确认你对「`res_zoom` 兜底溢出」的直觉是对的。
