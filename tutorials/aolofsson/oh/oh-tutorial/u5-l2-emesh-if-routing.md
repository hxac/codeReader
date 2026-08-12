# emesh 接口与路由

## 1. 本讲目标

上一讲（u5-l1）我们认识了 emesh 的「包」：一个 104 位的定长事务包，外加一对 `access`/`wait` 伴随信号。本讲要把视角从「一个包」拉远到「包在节点之间怎么流动」，也就是 **接口电路**。

读完本讲，你应该能够：

- 说清 `emesh_if.v` 如何在本地接口 `emesh` 与三个方向 `cmesh/rmesh/xmesh` 之间做**双向分发**，以及它真正用的路由依据是什么。
- 读懂 `emesh_mux.v` 这个 N 选 1 的**优先级多路选择器**：仲裁、选包、逐路反压三件事是怎么用一段组合逻辑写完的。
- 读懂 `emesh_decode.v` 这个**命令译码器**（注意：是「命令」译码，不是「地址」译码），并看出它里面的字段划分。
- 掌握贯穿这三个模块的同一套**分布式反压（backpressure）**写法，并能用真值表 + 德摩根律解释一条 ready 公式为何成立。

> 本讲承接 u5-l1（包格式与 `access`/`wait` 握手）、u3-l4（固定优先级仲裁器 `oh_arbiter`）。后面 u6（外设）与 u7（elink）都会反复复用本讲讲的「mux + 反压」范式。

## 2. 前置知识

在进入源码前，先用三段话补齐背景。

**片上网络与「接口电路」。** 把很多 IP 挂到一条总线上时，需要一些「路口」电路：把一个口的包分发给多个方向（分发 / 路由），或把多个口的包合并到一个口（仲裁 / 多路选择）。emesh 把这类路口电路做成了几个小模块。名字里的 `cmesh/rmesh/xmesh` 可以理解为「列（column）/行（row）/扩展（extension）」三个网格方向，`emesh` 则是本地节点接口。`emesh_if` 就是夹在「本地 emesh 口」与「三个网格方向」之间的十字路口。

**valid/ready 与 wait 的极性。** u5-l1 已确立协议层用 `wait` 表示反压——**高有效**表示「等一等、别走」，即 `~wait ≈ ready`。但本讲的 `emesh_if.v` 内部端口名直接叫 `ready`，且语义是 **active-high ready（高有效=可以接收）**，与 `wait` 正好互补：`ready ≈ ~wait`。这一点是本讲最容易绊倒人的地方，下文会反复强调，并指出仓库里接口命名存在历史漂移。

**固定优先级仲裁。** u3-l4 讲过 `oh_arbiter`：对每位请求构造等待掩码 `waitmask[j] = |requests[j-1:0]`，再 `grants = requests & ~waitmask`，得到 one-hot 的授权，bit0 优先级最高。`emesh_mux` 正是靠它来选包。

## 3. 本讲源码地图

| 文件 | 作用 | 是否被实例化 |
| --- | --- | --- |
| [emesh/hdl/emesh_if.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v) | emesh ↔ cmesh/rmesh/xmesh 的双向「十字路口」，纯组合直通逻辑 | 是，如 `elink/dv/dut_elink.v` |
| [emesh/hdl/emesh_mux.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_mux.v) | N 选 1 的优先级多路选择器（仲裁 + 选包 + 反压三合一） | 是，如 `spi/hdl/spi.v`、`gpio/hdl/axi_gpio.v` |
| [emesh/hdl/emesh_decode.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_decode.v) | 16 位命令字译码（写/读/原子操作/字段拆分） | 是，被 `emesh/hdl/emesh_pack.v` 实例化 |

> 提醒：这三个文件都受过仓库那次大重构（git log 里的 "Reorg!" 与 "Flattening directory tree"）影响，部分代码带有过渡痕迹。本讲一律「以源码实际文本为准」，对看得出来的不一致会就地指出。

## 4. 核心概念与源码讲解

### 4.1 路由：emesh_if 的双向分发

#### 4.1.1 概念说明

`emesh_if` 要解决的问题是：本地节点产生的 emesh 事务，该往哪个网格方向送？反过来，三个网格方向回来的事务，又该如何并回本地 emesh 口？所以它天然是**双向**的：

- **正向（emesh → cmesh/rmesh/xmesh）**：一发多，是「分发 / 路由」。
- **反向（cmesh/rmesh/xmesh → emesh）**：多发一，是「合并 / 优先级选择」。

文件第一行就标注了性质：

```verilog
//WARNING: Pass through logic
```

意思是这是**纯组合、直通（无寄存器）**的路口逻辑——包进来当拍就出去，没有缓冲。

#### 4.1.2 核心流程

正向分发的路由依据**不是地址位**，而是包里的**写位**（`packet_in[0]`，即 u5-l1 讲过的控制字节 bit0）：

```
emesh_access_in=1 且 packet_in[0]=1（写）  →  走 cmesh
emesh_access_in=1 且 packet_in[0]=0（读）  →  走 rmesh
xmesh                                        →  当前固定不驱动（access_out=0）
```

反向合并则用**固定优先级** cmesh > rmesh > xmesh 选一个包送往 emesh。

伪代码：

```
// 正向：emesh → 三个方向
cmesh_access_out = emesh_access_in &  packet_in[0]   // 写
rmesh_access_out = emesh_access_in & ~packet_in[0]   // 读
xmesh_access_out = 0                                 // 暂不用

// 反向：三方向 → emesh（优先级 cmesh > rmesh > xmesh）
emesh_packet_out = cmesh_access_in ? cmessh_packet :
                   rmesh_access_in ? rmesh_packet :
                                     xmesh_packet
```

> 注意：规格描述里写的是「access + 地址位决定方向」，但**当前源码的实际路由依据是写位（读/写分流），不是地址**。这是文档与代码的一处偏离，以代码为准。

#### 4.1.3 源码精读

参数照例是地址宽 `AW=32`、包宽 `PW=2*AW+40=104`：

[emesh/hdl/emesh_if.v:14-15](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L14-L15) 定义地址宽与包宽。

**正向分发**——按写位把事务导向 cmesh（写）或 rmesh（读），xmesh 暂不用；包本身则向三路原样广播：

[emesh/hdl/emesh_if.v:55-65](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L55-L65) 写位决定方向、包向三路广播。

正向的 ready 汇总：本地 emesh 只有在三个方向都「能收」时才算成功（见 4.4）：

[emesh/hdl/emesh_if.v:68-70](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L68-L70) 三个方向的 ready 相与，回送给 emesh 源端。

**反向合并**——先用一个三目嵌套做优先级选包（cmessh 优先），再由 ready 级联实现反压（见 4.4）：

[emesh/hdl/emesh_if.v:82-84](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L82-L84) 优先级选择反向回送的包。

> 一处待确认：反向的 `emesh_access_out = cmesh_access_in & rmesh_access_in & xmesh_access_in`（[第 76-78 行](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L76-L78)）用的是**与（&）**。这与紧随其后的优先级选择（任一路有请求即应有 access）以及实际例化（`dut_elink.v` 里 rmesh/xmesh 接 0）不太一致——若真是与，则 `emesh_access_out` 会恒为 0。考虑到文件顶部写着 "Pass through logic"、属过渡代码，这里疑似应为或（`|`）。**结论待本地仿真确认。**

#### 4.1.4 代码实践（源码阅读型）

**目标**：验证「正向路由依据是写位而非地址」。

1. 打开 [emesh/hdl/emesh_if.v:55-60](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L55-L60)。
2. 找到 `cmesh_access_out` 与 `rmesh_access_out` 两行。
3. 回答：决定方向的信号是 `emesh_packet_in[??]`？这一位在 u5-l1 里叫什么？
4. 再打开 `elink/dv/dut_elink.v`，看 `emesh_if` 的例化（搜索 `emesh_if #(.AW(AW))`），观察 `rmesh_access_in/xmess_access_in` 接的是什么常量。

**预期**：路由位是 `emesh_packet_in[0]`，即写位；例化中 rmesh/xmesh 的 access 接 `1'b0`，说明这条路口当前只用了 cmessh 一路。**无法在此断言运行结果，待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：若一个 emesh 写事务进来，`cmesh_ready_in=0` 但 `rmesh_ready_in=1`，正向能否成功？为什么？

**答**：能成功不必看 rmesh。因为写事务的 `cmesss_access_out=1`、`rmesh_access_out=0`，rmessh 这一路并无 access，其 ready 不参与该事务成败；只看 `cmesh_ready_in`。不过源码里 `emesh_ready_out` 是三路 ready 相与，因此实际能否成功取决于「不活跃的那几路是否恒返 ready=1」——**待本地验证**这种保守写法是否会误反压。

**练习 2**：为什么 `xmesh_access_out` 直接写死成 `1'b0`？

**答**：注释 `//Don't drive on xmesh for now` 说明 xmesh 方向当前未启用，是预留位。这是「先打通、再扩展」的渐进式写法。

---

### 4.2 多路选择与仲裁：emesh_mux

#### 4.2.1 概念说明

`emesh_if` 的反向合并只处理 3 路固定输入，且写死在文件里。`emesh_mux` 则是**参数化的 N 选 1 通用版本**：N 路请求进、1 路出，自带固定优先级仲裁，并给每一路单独的 ready。它是 gpio/spi 等外设把「多个内部源合并成一个 emesh 输出口」的标准积木——例如 `spi.v` 把主、从两路 SPI 的输出用 `emesh_mux #(.N(2))` 合并成一包。

#### 4.2.2 核心流程

`emesh_mux` 三步走：

1. **仲裁**：把 `access_in[N-1:0]` 当作 N 个请求，丢给 `oh_arbiter`，得到 one-hot 的 `grants[N-1:0]`（bit0 优先级最高）。参数 `CFG` 支持 `"STATIC"`（固定优先级，已实现）与 `"DYNAMIC"`（轮询，**仅有一句仿真期 `$display` 警告，未实现**）。
2. **选包**：用一个 `for` 循环把「被授权那一路的包」或起来。因 `grants` 是 one-hot，等价于一个多路选择器。
3. **反压**：每一路单独算 ready——只有「被授权 且 下游 ready」的那一路才拿到 ready=1，其余请求方被反压。

```
grants      = oh_arbiter(access_in)            // one-hot
access_out  = |access_in                        // 任一路有请求
packet_out  = OR_i ( grants[i] ? packet_in[i] ) // 选中被授权的包
ready_out[i]= ~(access_in[i] & ~grants[i]) & ready_in   // 逐路反压
```

#### 4.2.3 源码精读

参数与接口——注意默认 `N=99`（实例化时再覆盖，如 spi 里 `N=2`）：

[emesh/hdl/emesh_mux.v:11-14](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_mux.v#L11-L14) 参数：地址宽、包宽、路数 N、仲裁模式 CFG。

仲裁器实例——`CFG=="STATIC"` 分支调用 stdlib 的 `oh_arbiter`；`"DYNAMIC"` 分支只在仿真期打印「未实现」：

[emesh/hdl/emesh_mux.v:42-58](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_mux.v#L42-L58) STATIC 用 oh_arbiter；DYNAMIC 是占位桩。

输出有效与逐路 ready：

[emesh/hdl/emesh_mux.v:62-65](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_mux.v#L62-L65) `access_out` 是请求的或；`ready_out` 是逐路反压。

参数化选包循环——`(grants[i] ? packet_in[i] : 0)` 用复制 + 按位或实现：

[emesh/hdl/emesh_mux.v:68-73](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_mux.v#L68-L73) for 循环选包。

> 一处源码缺陷：第 65 行
> ```verilog
> assign ready_out[N-1:0] = ~(access_in[N-1:0] & ~grants[N-1:0]) & {(N){ready_in}});
> ```
> 末尾多了一个右括号（`{(N){ready_in}})`），括号不匹配，**按字面无法编译**。其语义意图很清楚，应是 `~(access_in & ~grants) & {N{ready_in}}`。这是仓库历史遗留，读源码时心里纠正即可；**待本地验证修复后的行为**。

`spi.v` 里真实使用 N=2，把从机 `s_*` 与主机 `m_*` 两路输出合并：

[spi/hdl/spi.v:125-136](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi.v#L125-L136) 用 emesh_mux 合并主从两路 SPI 输出。

#### 4.2.4 代码实践（源码阅读型）

**目标**：验证「选包循环 + one-hot grant」等价于一个多路选择器。

1. 读 [emesh_mux.v:68-73](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_mux.v#L68-L73) 的 `for` 循环。
2. 假设 N=2、`grants=2'b10`（即 bit1 被授权），手算 `packet_out`。注意 `packet_in[((i+1)*PW-1)-:PW]` 取的是第 i 路的 PW 位。
3. 回答：若 `grants=2'b00`（无人请求），`packet_out` 等于什么？

**预期**：`grants=2'b10` 时 `packet_out = packet_in[第1路]`；`grants=2'b00` 时 `packet_out = 'b0`（全 0）。这正是一个 one-hot 多路选择器。**待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：`access_out = |access_in`，为什么用「或」而不是「与」？

**答**：`access_out` 表示「输出口当前有没有有效事务」。任意一路有请求（`access_in` 任一为 1）都应让输出口有效，所以是或。若是与，则要求所有路同时请求才有效，违背多路选择的本意。

**练习 2**：`CFG=="DYNAMIC"` 真的能做轮询仲裁吗？

**答**：不能。该分支只在 `TARGET_SIM` 下 `$display` 打印一句 "ROUND ROBIN ARBITER NOT IMPLEMENTED"，并没有实例化任何仲裁器，`grants` 会悬空。轮询公平仲裁需记忆状态（见 u3-l4），此处未实现。

---

### 4.3 命令译码：emesh_decode

#### 4.3.1 概念说明

规格里把这个模块标作「地址译码」，但**它其实是「命令译码器」**——输入是一个 16 位的命令字 `cmd_in`，输出是一组「这是什么操作」的标志位（写、读、各种原子、CAS）和拆好的字段（opcode/length/size/user）。它被 `emesh_pack.v` 用来在**打包**时判断当前事务是不是写事务（从而决定包里某段放 data 还是 srcaddr）。

#### 4.3.2 核心流程

16 位命令字的字段划分（来自 `emesh_pack.v` 的拼装）：

| 位段 | 字段 | 含义 |
| --- | --- | --- |
| `cmd_in[3:0]` | opcode | 操作码（写/读/原子） |
| `cmd_in[7:4]` | length | 长度 |
| `cmd_in[10:8]` | size | 数据宽度 |
| `cmd_in[15:11]` | user（高 5 位） | 用户自定义 |

译码就是两件事：一是按 opcode 译出各类操作脉冲，二是把字段直通拆出来。

#### 4.3.3 源码精读

端口声明——注意输出里有一个 `cmd_cas`：

[emesh/hdl/emesh_decode.v:9-28](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_decode.v#L9-L28) 命令译码器的端口（写、读、各原子操作、字段）。

写指示与读/原子译码：

[emesh/hdl/emesh_decode.v:35-44](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_decode.v#L35-L44) `cmd_write=~cmd_in[3]`；各操作按 opcode 译码。

字段拆分直通：

[emesh/hdl/emesh_decode.v:46-50](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_decode.v#L46-L50) opcode/length/size/user 直接按位段取出。

> 两处源码缺陷，读时务必留意：
>
> 1. **端口名不匹配**：第 20 行声明的输出叫 `cmd_cas`，但第 40 行赋值的是 `cmd_atomic_cas`（端口表里并无此名）。于是 `cmd_atomic_cas` 成了一根悬空的隐式 1 位线，而端口 `cmd_cas` 没人驱动。下游 `emesh_pack.v` 对 CAS 输出留空（`.cmd_cas()`），所以当前不影响打包，但 CAS 功能实际是断的。
> 2. **二进制字面量写成了十进制**：`cmd_in[3:0]==1011` 这类比较里，`1011` 是**裸字面量**，Verilog 会按**十进制 1011** 解释，而不是二进制 `4'b1011`。4 位向量最大值才 15，故这些分支（read、write_stop、各原子）**恒为假**。作者显然想表达二进制操作码（1000=读、1011=CAS、1100=ADD…），但写法有误。**好消息**：`emesh_pack.v` 实际只取 `cmd_write = ~cmd_in[3]`（[第 108-123 行](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_pack.v#L108-L123) 里其余输出都留空），所以这条「写判定」不受影响，打包能正常工作。其余译码分支属未启用/待修复。**结论待本地仿真确认。**

#### 4.3.4 代码实践（源码阅读型）

**目标**：看清译码器在真实链路里只用了「写指示」这一根线。

1. 打开 [emesh/hdl/emesh_pack.v:101-123](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_pack.v#L101-L123)。
2. 看 `cmd_out` 是怎么由 `opcode_in/length_in/size_in/user_in` 拼出来的（第 103-106 行）。
3. 看 `emesh_decode` 的例化：除 `cmd_write` 外，其它输出是不是都接了 `()`？
4. 回答：为什么注释敢写 "only write indicator needed"？

**预期**：打包阶段只需区分「写 vs 非写」来决定某段放 data 还是 srcaddr，因此只消费 `cmd_write`，复杂的原子译码在此链路里并未被使用。**待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：`cmd_write = ~cmd_in[3]`，说明「写」的 opcode 特征是什么？

**答**：只要 opcode 的最高位（bit3）为 0 就算写。也就是说 opcode 空间被一刀切：bit3=0 → 写类，bit3=1 → 非写（读/原子）。这是一个最省事的写/读分流。

**练习 2**：若想让 `cmd_read` 真正在 `cmd_in[3:0]==4'b1000` 时拉高，源码要怎么改？

**答**：把裸字面量改成带基数的二进制字面量：`assign cmd_read = (cmd_in[3:0]==4'b1000);`，并把 `cmd_atomic_cas` 改回端口名 `cmd_cas`（或反过来统一端口名）。

---

### 4.4 分布式反压：ready 信号的逻辑

#### 4.4.1 概念说明

「反压（backpressure）」是总线/网络的命脉：当下游来不及处理时，必须及时告诉上游「等一等」，否则丢事务。emesh 把反压做成了**分布式、纯组合**的——每个路口都用一两行布尔式，把下游的 ready 与本地的仲裁结果糅在一起，回送给每个上游。

`emesh_if` 与 `emesh_mux` 用的是**同一套 ready 写法**，只是规模不同：

- 输出侧的 `ready_out`（回送给上游）= 「我没在往一个吃不消的下游送」。
- 把「优先级」也编进 ready：低优先级者只有在「高优先级者都没请求」时才能拿到 ready。

#### 4.4.2 核心流程

先看最高优先级那一路（cmesh）的 ready：

\[ \texttt{cmesh\_ready\_out} = \neg(\texttt{cmesh\_access\_in} \land \neg\texttt{emesh\_ready\_in}) \]

由德摩根律等价于：

\[ \texttt{cmesh\_ready\_out} = \neg\texttt{cmesh\_access\_in} \lor \texttt{emesh\_ready\_in} \]

直觉：「我没有请求」或「下游能收」——满足任一，就告诉 cmessh「你准备好了」。

再看低优先级者如何被「让路」——`rmesh` 要额外要求 cmesh 没在请求，`xmesh` 要额外要求 cmesh、rmesh 都没在请求：

[emesh/hdl/emesh_if.v:86-93](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L86-L93) 三路 ready 的级联，优先级 cmesh > rmesh > xmesh。

`emesh_mux` 的逐路 ready 是同一思想的参数化版：

\[ \texttt{ready\_out}[i] = \neg(\texttt{access\_in}[i] \land \neg\texttt{grants}[i]) \land \texttt{ready\_in} \]

即「我这一路要么没请求、要么被授权了，并且下游能收」——丢了仲裁的请求方 ready=0，被反压住。

#### 4.4.3 源码精读

`emesh_if` 反向三级 ready 级联（cmessh 不必让任何人；rmesh 要让 cmessh；xmesh 要让 cmessh 和 rmesh）：

[emesh/hdl/emesh_if.v:86-93](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L86-L93) 三路 ready 级联。

`emesh_mux` 的逐路 ready（含前述括号笔误，读时心里补正）：

[emesh/hdl/emesh_mux.v:65](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_mux.v#L65) 逐路反压公式。

#### 4.4.4 代码实践（本讲主实践）

**目标**：用真值表说清楚 `cmesh_ready_out = ~(cmesh_access_in & ~emesh_ready_in)` 为何是一条正确的反压式。

1. 打开 [emesh/hdl/emesh_if.v:86](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L86)。
2. 列出 `cmesh_access_in`、`emesh_ready_in` 两种取值的全部组合，算出 `cmesh_ready_out`：

| `cmesh_access_in` | `emesh_ready_in` | `cmesh_ready_out` | 含义 |
| :-: | :-: | :-: | :-- |
| 0 | 0 | 1 | 没请求，ready 无意义，给 1（不挡路） |
| 0 | 1 | 1 | 没请求，给 1 |
| 1 | 1 | 1 | 有请求且下游能收 → 放行 |
| 1 | 0 | **0** | 有请求但下游收不下 → 反压 |

3. 用德摩根律把原式改写：`~(A & ~R) = ~A | R`，与上表对照。

**预期/答案**：该式仅在「我正在送（access=1）且下游吃不消（ready=0）」时才把 ready 拉低，其余情况都给 ready=1。这正是「把下游反压如实、且仅在必要时回传给上游」的最小写法。又因 cmessh 优先级最高，它无须参考 rmesh/xmesh；优先级更低的 rmesh、xmesh 才在各自式子里额外 AND 进 `~cmesh_access_in`（、`~rmesh_access_in`），以便在高优先级者发言时主动让路。于是「反压」与「优先级」被同一组 ready 式一并表达。**本结论由源码文本与布尔推演得出，波形层面待本地仿真确认。**

#### 4.4.5 小练习与答案

**练习 1**：`rmesh_ready_out` 比 `cmessh_ready_out` 多了 `~cmesh_access_in` 这一项，去掉它会怎样？

**答**：去掉后 rmesh 不再「让」cmesh。当 cmessh 与 rmesh 同拍都请求、而下游只能收一个时，两路都可能拿到 ready，导致两个包挤进同一拍、输出冲突。这一项正是用反压实现固定优先级的关键。

**练习 2**：`emesh_mux` 的 `ready_out[i]` 里为什么要有 `& ready_in`？

**答**：即便第 i 路赢了仲裁（`grants[i]=1`），若下游输出口反压（`ready_in=0`），它仍不能走，故必须再 AND 下游 ready。`ready_in` 是对所有路的公共下游反压，`~(access_in & ~grants)` 是「仲裁输赢」，两者合起来才是每一路真正的「可否放行」。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一次「纸面追踪」：

**任务**：一个 emesh 读事务从本地 `emesh` 口进入 `emesh_if`，请追踪它在「正向分发」里走过的每一行代码，并说明为什么它不会被打到 cmesh。

1. 读事务意味着 `emesh_packet_in[0] = 0`（写位为 0）。
2. 在 [emesh_if.v:55-60](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L55-L60)：`cmesss_access_out = emesh_access_in & packet_in[0] = ... & 0 = 0`（不打到 cmessh），`rmesh_access_out = emesh_access_in & ~packet_in[0] = ... & 1`（打到 rmesh）。
3. 反过来，若有多个网格方向同时回送事务，`emesh_if` 的反向 [优先级选包（第 82-84 行）](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L82-L84) 与 [ready 级联（第 86-93 行）](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_if.v#L86-L93) 会保证 cmessh > rmessh > xmesh 的顺序、且不丢反压。
4. 若这包接下来要进一个有多内部源的外设（如 spi），则会再经过一个 `emesh_mux`（[spi.v:125-136](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi.v#L125-L136)），用同一套「仲裁 + 选包 + 逐路 ready」范式合并。

**交付**：画一张含 `emesh`、`emesh_if`、`cmessh/rmesh/xmesh`、`emesh_mux` 的小框图，标注一个读事务的走向与每一段的 ready 极性（active-high ready）。**运行层面待本地仿真确认。**

## 6. 本讲小结

- `emesh_if` 是 emess 与 cmesh/rmesh/xmesh 三个方向之间的**纯组合双向路口**；正向按**写位（读/写分流）**分发，反向用**固定优先级**合并——注意实际路由依据是写位，而非地址位。
- `emesh_mux` 是参数化的 **N 选 1 优先级多路选择器**：`oh_arbiter` 出 one-hot grant、`for` 循环选包、逐路算 ready，三件事用一段组合逻辑写完；轮询（DYNAMIC）未实现。
- `emesh_decode` 是**命令译码器**（不是地址译码），把 16 位命令字译成写/读/原子标志与字段；当前链路只用了 `cmd_write`。
- 反压是**分布式、纯组合**的：每条 `ready_out` 把「下游 ready」与「本路口的仲裁结果/优先级」糅在一起，用 `~(access & ~grant)` 这类式子回送上游——反压与优先级被同一组 ready 式表达。
- 三个文件都带仓库大重构的过渡痕迹（`ready`↔`wait` 命名漂移、`emesh_mux` 第 65 行括号笔误、`emesh_decode` 的端口名/二进制字面量问题、`emesh_if` 反向 `access_out` 的与/或疑似笔误），**一律以源码文本为准、关键结论待本地仿真确认**。

## 7. 下一步学习建议

- 下一讲 **u5-l3（包的打包、解包与回读）** 会讲 `emesh_pack/emesh_unpack/wralign/rdalign/readback`——本讲提到的 `emesh_decode` 正是被 `emesh_pack` 调用的，届时你会看清「命令字如何拼进 104 位包」。
- 想看 `emesh_if` 真实接线，可读 `elink/dv/dut_elink.v` 中 `emesh_if` 的例化；想看 `emesh_mux` 的典型用法，可读 `spi/hdl/spi.v`、`gpio/hdl/axi_gpio.v`。
- 进入第 6 单元（外设）后，你会发现 gpio/spi 等几乎每个外设的输出侧都是一个 `emesh_mux`——届时可回头对照本讲的「仲裁 + 选包 + 逐路 ready」范式。
