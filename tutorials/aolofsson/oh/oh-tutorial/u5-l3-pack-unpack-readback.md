# 包的打包、解包与回读

## 1. 本讲目标

上一讲（u5-l1）我们认识了 emesh 事务「包」本身——一个 104 位的定长比特串，以及 `access/wait` 握手。但工程里没有人愿意手写 104 位裸比特：写代码时我们想用「目标地址 `dstaddr`、数据 `data`、是不是写 `write`」这些**有名字的字段**，在线上传输时又必须把它们压成一个**扁平的包**。

本讲就来填上「字段 ⇄ 扁平包」之间的所有转换环节。学完后你应当能够：

- 说清 **pack/unpack** 在做什么：把结构化字段拼成包、把包拆回字段，以及它为什么用 `generate` 按位宽分支。
- 说清 **wralign/rdalign** 在做什么：子字访问（字节/半字/字）时，写数据如何在字节通道间**复制广播**、读数据如何被**抽取对齐**。
- 说清 **readback** 在做什么：一个读请求进来，从内部寄存器取到数据后，如何把它**塞回响应包**、并把响应送回正确的请求者。
- 串通一条完整链路：**gpio 读 `GPIO_IN` 时，引脚电平如何最终回到 `packet_out`**。

## 2. 前置知识

本讲默认你已经学完：

- **u5-l1（emesh 包格式与协议）**：知道包宽 \(PW = 2\,AW + 40\)，AW=32 时 PW=104；知道控制字节里 `write`(bit0)、`datamode`(bits[2:1]) 的含义；知道 `access/wait` 握手。
- **u2-l1（组合逻辑原语）**：看得懂 `{(8){sel}} & data` 这种「把 1 位选择信号复制成 8 位掩码，再和字节相与」的 AND-OR 选择写法。
- **u2-l2（时序原语）**：看得懂 `always @(posedge clk)` 打一拍的寄存器，以及 `if(ready_in)` 这种反压门控。

两个贯穿全讲的底层直觉：

1. **包就是一个定长的整数**。所谓「打包」就是用位拼接（`{}`）或位切片赋值（`assign packet[hi:lo] = field`）把几个小整数排进一个大整数；「解包」就是反过来切片。没有任何魔法。
2. **「源地址」与「写数据」共用同一片比特**。这在 u5-l1 已经埋下伏笔：写事务时这片比特是数据，读请求时这片比特是回信地址。本讲会看到 pack/unpack/readback 都在围绕这一点做文章。

> 阅读提醒（承接前几讲的家规）：OH! 仓库经历过一次尚未完成的接口迁移。`emesh_pack.v` / `emesh_unpack.v` 是较新的「扩展命令」版本，而 `emesh_readback.v` 和 `gpio.v` 里实例化的 `enoc_unpack` / `enoc_pack` 引用的是更早的 `packet2emesh.v` 接口（端口名 `write_in/datamode_in/...`），该文件已不在仓库中。所以**这些模块的「设计意图」是清晰且自洽的，但并非都能原样编译连接**。本讲一律以源码文本为准，把这种漂移在对应处点明。

## 3. 本讲源码地图

| 文件 | 作用 | 被谁使用 |
|------|------|----------|
| [emesh/hdl/emesh_unpack.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_unpack.v) | 包 → 字段：把 `packet_in` 切成 `cmd/dstaddr/srcaddr/data` 等 | `emesh_memory.v` |
| [emesh/hdl/emesh_pack.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_pack.v) | 字段 → 包：把命令与地址数据拼成 `packet_out` | `emesh_memory.v` |
| [emesh/hdl/emesh_decode.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_decode.v) | 命令字译码：从 16 位 `cmd` 译出 `cmd_write/cmd_read/size/...` | pack / unpack 内部 |
| [emesh/hdl/emesh_wralign.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_wralign.v) | 写对齐：把 LSB 对齐的写数据**复制**到所有字节通道 | （当前无实例，读侧对偶） |
| [emesh/hdl/emesh_rdalign.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_rdalign.v) | 读对齐：把读回数据按地址/宽度**抽取**到低位 | `emesh_memory.v` |
| [emesh/hdl/emesh_readback.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_readback.v) | 读响应装配：取请求者地址 + 读数据，重组响应包 | `gpio.v` |
| [gpio/hdl/gpio.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v) | 综合范例：外设如何用 unpack + readback 完成一次读 | — |

---

## 4. 核心概念与源码讲解

### 4.1 pack / unpack：字段与扁平包的互转

#### 4.1.1 概念说明

线上跑的是一串比特，人脑里想的是几个字段。`pack`（打包）和 `unpack`（解包）就是两者间的双向翻译器，且**互为逆运算**：

- **unpack**：进一个 `packet_in`，出若干有名字的字段（`write/datamode/dstaddr/srcaddr/data...`）。放在事务流的**入口**——从机收到包后先 unpack，才能知道「这是要读还是写、写到哪个地址」。
- **pack**：反过来，进若干字段，出一个 `packet_out`。放在事务流的**出口**——主机要发请求、从机要回响应时，靠 pack 把字段压回一根线。

为什么这件事值得单独做成原语？因为**位宽要参数化**。emesh 的地址宽 `AW` 可变（16/32/64/128），包宽 `PW` 随之变化，字段在包里的位置也会变。如果把切片写死在每个调用方，改一次位宽要改几十处；抽成 pack/unpack 后，调用方只管「字段」，位宽适配集中在一个文件里。

#### 4.1.2 核心流程

把 AW=32 的简单情形画清楚（这是 u5-l1 的 104 位格式，也是 gpio/elink 实际交换的格式）：

```
            bit0                                          bit103
packet_in = [ ctrl(8) | dstaddr(32) | data/srcaddr(32) | srcaddr(32) ]
                ↓ unpack 切片
fields    = { write, datamode, ctrlmode,   ← 来自 ctrl
              dstaddr,                       ← bits[47:16]
              data,                          ← bits[79:48]（写时）
              srcaddr }                      ← bits[79:48]（读时）/ bits[103:80]
```

关键两点：

1. **`data` 与 `srcaddr` 占同一片比特**（bits[79:48]）。写事务时它是数据，读请求时它是回信地址。所以 unpack 对这两个字段常取**同一段切片**，由调用方按 `write` 决定哪个有意义。
2. **控制信息在最前面（低位）**。这样接收端不必解析完整包，只看前几 bit 就能分流读/写。

pack 就是把上图**从下往上**再拼一遍。

> 真实文件 `emesh_pack.v` / `emesh_unpack.v` 把这件事做得更通用：它们用一个 **16 位的命令字** `cmd[15:0]`（含 `opcode/length/size/user`，支持突发与原子操作），并用 `generate` 对 AW∈{16,32,64,128}、PW 多档分别给出切片。但核心动作完全一样——按固定边界切片/拼接。下面 4.1.3 精读就用它真实的 AW=32 分支。

#### 4.1.3 源码精读

**(a) 命令字的拼装与译码**

pack 先把四个小命令字段拼成 16 位 `cmd_out`：

[emesh/hdl/emesh_pack.v:103-106](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_pack.v#L103-L106) — 把 `opcode/length/size/user` 拼进 `cmd_out[15:0]`，再交给 `emesh_decode` 译码。

`emesh_decode` 是命令字的「含义翻译器」，核心两条：

[emesh/hdl/emesh_decode.v:35](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_decode.v#L35) — `cmd_write = ~cmd_in[3]`：opcode 的 bit3 为 0 即写。
[emesh/hdl/emesh_decode.v:39](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_decode.v#L39) — `cmd_read = (cmd_in[3:0]==1000)`：opcode 恰为 `1000` 才是读。

> 注意：`decode` 按 4 位 opcode 区分写起/写停/读/CAS/原子加减与或非等，是「扩展命令」全集；而 gpio 走的旧接口（4.3 节）只用最简单的 1 位 `write`。两套并存，是仓库迁移的痕迹。

命令字段位序由这两行一锤定音：

[emesh/hdl/emesh_decode.v:47-50](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_decode.v#L47-L50) — `opcode=[3:0]`、`length=[7:4]`、`size=[10:8]`、`user=[15:8]`。

**(b) 打包：字段 → 包（AW=32 的 112 位扩展档）**

看一段真实的「字段拼成包」：

[emesh/hdl/emesh_pack.v:151-156](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_pack.v#L151-L156) — AW=32、PW=112 档：
`packet[15:0]=cmd`、`packet[47:16]=dstaddr`、`packet[79:48]= cmd_write? data[31:0] : srcaddr[31:0]`、`packet[111:80]=data[63:32]`。

第三段那句三目运算正是 4.1.2 强调的「数据/源地址共用同一片比特」——**写则填数据，读则填回信地址**。

**(c) 解包：包 → 字段（同一档的逆运算）**

[emesh/hdl/emesh_unpack.v:88-92](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_unpack.v#L88-L92) — 把 `packet_in` 按完全相同的边界切回：`cmd=packet[15:0]`、`dstaddr=packet[47:16]`、`srcaddr=packet[79:48]`、`data[63:0]=packet[111:48]`。

注意 `srcaddr` 与 `data` 都从 `[79:48]` 取——和 pack 那段三目运算一一对应。pack 和 unpack 的边界表必须**严格对齐**，否则一个 bit 错位整条链路就乱。这也是为什么把它们做成一对原语、放在同一个目录里维护。

> 真实文件默认参数是 `AW=64, PW=144`（见 [emesh_pack.v:82-83](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_pack.v#L82-L83)），并**没有** PW=104 这一档：104 位/8 位控制字节的简单格式由旧接口 `packet2emesh.v`（即 gpio 里的 `enoc_unpack`/`p2e`）处理，该文件已不在仓库。理念一致，格式不同——以源码为准。

#### 4.1.4 代码实践

**目标**：亲手验证 pack 与 unpack 是互逆的位拼接/切片。

**操作步骤**（源码阅读 + 纸面演算）：

1. 打开 `emesh/hdl/emesh_unpack.v` 的 AW=32、PW=112 分支（88–92 行），抄下四段切片边界。
2. 设定一组字段：`cmd=16'h0008`（opcode=1000 → 读）、`dstaddr=32'h80800004`、`srcaddr=32'h00000010`、`data=64'h0`。
3. 用 pack 的同档边界（151–156 行）把它们拼成一个 112 位 `packet`。
4. 再用 unpack 的边界把这个 `packet` 切回去，核对是否得到原始字段。

**需要观察的现象**：因为写位为 0（读），pack 会把 `srcaddr` 放进 bits[79:48]；unpack 切 bits[79:48] 既能当 `srcaddr` 也能当 `data`——你应当看到「同一片比特被两个字段读走」。

**预期结果**：拼出来再切回去，`cmd/dstaddr/srcaddr` 与原始值逐位相等；`data` 取到的是 `srcaddr` 的值（对读事务而言，data 字段无意义）。无需运行仿真即可完成；如要仿真验证，可仿照 u4-l3 写一个最小 dut 包装，但本仓库脚本缺 stdlib 搜索路径（见 u1-l3），**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 pack/unpack 要用 `generate` 对不同 `AW/PW` 分支，而不是写一个通用公式？

> **答**：因为不同位宽下，`dstaddr/data/srcaddr` 的段长度和相对位置都变（AW=16 时甚至没有独立 srcaddr）。硬套一个公式要么浪费比特、要么越界。`generate` 为每种合法组合显式给出切片表，并对接不合法的组合用 `$display` 报错（见各分支末尾的 `perror`），清晰且安全。

**练习 2**：`emesh_decode.v` 里 `cmd_write = ~cmd_in[3]`，意味着哪些 opcode 会被当成「写」？

> **答**：凡是 bit3=0 的 opcode 都算写（0000~0111），bit3=1 的 1000 是读，1011~1111 是 CAS/原子。换言之 bit3 是「读/写」的分水岭，bit2:0 再细分具体子类型。

---

### 4.2 wralign / rdalign：子字访问的字节通道对齐

#### 4.2.1 概念说明

emesh 总线一次最多搬 64 位（双字），但事务允许只写/读**一个字节（8 位）**或**半字（16 位）**。问题来了：

- **写一个字节**时，CPU 给的 8 位数据放在哪？是 `data[7:0]` 还是 `data[31:24]`？下游存储又怎么知道该把这 8 位写进哪个字节？
- **读一个字节**时，存储体一次吐出 64 位，CPU 怎么从中挑出它要的那 8 位并放回低位？

OH! 的选择是**复制广播 + 地址选取**：

- **写侧 `emesh_wralign`**：约定写数据**永远 LSB 对齐**（字节就在 `data[7:0]`）。wralign 把这 8 位**复制到 64 位的每一个字节通道**，于是无论地址指向哪一字节，正确数据都已「就位」，下游用地址选即可。
- **读侧 `emesh_rdalign`**：存储体吐出 64 位，rdalign 根据地址和宽度，把目标字节/半字**抽到低位**，让 CPU 拿到的永远是 LSB 对齐的结果。

两者是一对方向相反的「字节转向」电路，是所有支持子字访问的存储型从机（如 `emesh_memory`）的标配。注意 **gpio 用不到它们**：gpio 的寄存器是整 N 位一起读写，没有「只写一个字节」的需求。

#### 4.2.2 核心流程

**wralign（写复制广播）**，以「字节写、数据 = `0xAB`」为例：

```
datamode=00(字节), data_in = 0x00000000000000AB
            ↓ 把 in[7:0] 复制到 8 个字节通道
data_out  = 0xABABABABABABABAB
```

每个输出字节都是一个多路选择：「如果是字节模式就取 `in[7:0]`，如果是半字就取 `in[15:8]`，如果是字就取 `in[31:24]`，如果是双字就取 `in[63:56]`」。模式互斥，正好填满。

**rdalign（读抽取对齐）**，以「字节读、地址 `addr[2:0]=3'b101`」为例（要读的是高字的第 1 字节）：

```
1) addr[2]=1 → 选高字:  data_mux = data_in[63:32]
2) addr[1:0]=01 → 选该字的字节1: data_out[7:0] = data_mux[15:8]
3) 其余字节按 datamode 决定是否填到 [15:8]/[31:24]
```

net 效果：目标字节被搬到 `data_out[7:0]`，CPU 直接读低位即可。

#### 4.2.3 源码精读

**(a) wralign：模式译码与字节复制**

先看模式译码（`datamode` 两位 → `data_size` 四位独热）：

[emesh/hdl/emesh_wralign.v:17-20](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_wralign.v#L17-L20) — `00=字节、01=半字、10=字、11=双字`，译成独热的 `data_size[3:0]`。

再看最高字节 `B7` 如何被填满——这就是「复制」发生的现场：

[emesh/hdl/emesh_wralign.v:53-56](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_wralign.v#L53-L56) — `B7 = (字节?in[7:0]) | (半字?in[15:8]) | (字?in[31:24]) | (双字?in[63:56])`。

`{(8){data_size[k]}}` 正是 u2-l1 讲过的「1 位选择复制成 8 位掩码」套路：模式命中哪一路，就把对应源字节广播到 `B7`。其余 `B0..B6`（23–56 行）同理，各自按模式选源。8 个字节合起来，就是完整的复制图案。

**(b) rdalign：先选字、再选字节**

第一步，用 `addr[2]` 在高低字之间二选一：

[emesh/hdl/emesh_rdalign.v:56](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_rdalign.v#L56) — `data_mux = addr[2] ? data_in[63:32] : data_in[31:0]`。

第二步，用 `addr[1:0]` 在所选字的 4 个字节里独热选一，送往输出字节 0：

[emesh/hdl/emesh_rdalign.v:60-63](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_rdalign.v#L60-L63) — `byte0_sel` 是 `addr[1:0]` 的独热译码。
[emesh/hdl/emesh_rdalign.v:79-82](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_rdalign.v#L79-L82) — 据此把选中字节接 到 `data_aligned[7:0]`。

第三步，高 32 位原样直通（双字读才需要）：

[emesh/hdl/emesh_rdalign.v:97](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_rdalign.v#L97) — `data_out[63:32] = data_in[63:32]`（pass-through）。

**(c) 谁在用它**

- 读对齐有真实消费者：[emesh/hdl/emesh_memory.v:191](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_memory.v#L191) — 片上 RAM 从机在回读数据上挂了 `emesh_rdalign`。
- 写对齐 `emesh_wralign` 当前**无任何实例**（全仓库搜索仅命中其自身定义）。它是 rdalign 的对偶设计，作为「写侧字节广播」的模板保留，实际写通道路径尚未接入——**以源码为准，待确认**。

#### 4.2.4 代码实践

**目标**：用纸面演算验证 wralign 的「复制」与 rdalign 的「抽取」互为逆过程。

**操作步骤**：

1. 设 `data_in = 0x00000000000000CD`、`datamode=2'b00`（字节写）。按 wralign 的 B0…B7 公式写出 `data_out`。
2. 把得到的 `data_out` 当作存储体内容，再做一个「字节读」：`addr[2:0]=3'b000`、`datamode=2'b00`，按 rdalign 公式算 `data_out_rd[7:0]`。

**需要观察的现象**：步骤 1 应得到 `0xCDCDCDCDCDCDCDCD`；步骤 2 应从其低字取出字节 0。

**预期结果**：`data_out_rd[7:0] = 0xCD`，与原始写入字节一致——证明广播再抽取能还原。若改 `addr[1:0]`，取出的字节值仍应是 `0xCD`（因为广播后每个字节都一样），这正是广播设计的目的。无需仿真，纸面即可；**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 wralign 要把字节「广播到所有通道」，而不是只放到 `data[7:0]` 再配一个字节使能？

> **答**：广播后，下游无论用哪条字节通道、用地址选哪一字节，都能直接拿到正确值，简化了从机的写使能逻辑（不必再按地址做字节转向）。代价是总线上的冗余翻转，但对低速存储型 IO 可接受。

**练习 2**：rdalign 的 `data_out[63:32]` 为什么是 pass-through 而不是也做对齐？

> **答**：只有双字（64 位）读才会用到高 32 位，而双字读本身不需要字节级偏移（地址低 3 位应为 0）。所以高 32 位直接透传；字节/半字/字读只影响低 32 位，由 `data_mux` + `byte0_sel` 处理。

---

### 4.3 readback：把内部寄存器值塞回响应包

#### 4.3.1 概念说明

读事务是「一来一回」：请求方发一个读包（带目标地址 + 自己的回信地址 `srcaddr`），从机取出数据后，必须**回一个响应包**。这个响应包要解决三个问题：

1. **发往哪里？**——响应的目标地址，应是请求方的 `srcaddr`（回信地址），而不是原来的 `dstaddr`。
2. **数据是什么？**——从机内部寄存器/存储读出的值。
3. **怎么表示这是响应？**——读响应在 emesh 里被当作一次「写」送回请求方（`write=1`），因为对请求方而言，「收到回写的数据」和「一次写事务」长得一样。

`emesh_readback` 就是干这三件事的小流水线：**解包读请求 → 交换地址 → 注入读数据 → 重新打包成响应**。它只处理读（`~write_in`），写事务在它这里被吞掉（`access_out <= access_in & ~write_in`），因为写不需要回数据。

它是 gpio（以及任何「寄存器映射型外设」）回读路径的标准件。

#### 4.3.2 核心流程

readback 是**单级流水线**（打一拍），所有寄存器更新都受 `ready_in` 门控（反压：下游没准备好就不推进）：

```
输入: access_in, packet_in(读请求), read_data[63:0](调用方预先取好的数据), ready_in
        │
        ▼  enoc_unpack(p2e): 解包 packet_in
   { write_in, datamode_in, ctrlmode_in, dstaddr_in(=目标), srcaddr_in(=回信) }
        │
        ▼  时钟沿, 若 ready_in:
   access_out <= access_in & ~write_in        // 只放行读
   dstaddr_out <= srcaddr_in                  // ★ 响应目标 = 请求者回信地址
   datamode_out/ ctrlmode_out <= 直通
   data_out <= read_data[31:0]                // ★ 注入读数据(低32位)
   srcaddr_out <= read_data[63:32]            // 高32位(可作高数据/源)
        │
        ▼  enoc_pack(e2p): 重新打包, write_out 恒为 1
输出: access_out, packet_out(响应包), ready_out = ready_in
```

整条路径的精髓是 `dstaddr_out <= srcaddr_in` 这一句——**地址回送**。读请求里的「我是谁、回信地址在哪」被搬到了响应包的「送往何处」，于是响应能准确回到请求者。`data_out` 直接取 `read_data[31:0]`，把外设预先准备好的寄存器值贴进响应包的数据字段。

#### 4.3.3 源码精读

**(a) 解包读请求**

readback 内部先实例化解包器（实例名 `p2e` = packet→emesh）：

[emesh/hdl/emesh_readback.v:48-59](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_readback.v#L48-L59) — 解包 `packet_in` 得到 `write_in/datamode_in/ctrlmode_in/dstaddr_in/srcaddr_in/data_in`。

> 这里的实例名是 `enoc_unpack`，端口是 `write_in/datamode_in/...`，对应已不在仓库的旧 `packet2emesh.v` 接口，**与现行 `emesh_unpack.v`（`cmd_write/cmd_size/...`）端口名不一致**——这是迁移痕迹，读意图即可。

**(b) 流水线：地址回送 + 数据注入**

两条 `always` 完成核心动作：

[emesh/hdl/emesh_readback.v:66-70](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_readback.v#L66-L70) — `access_out` 在复位后为 0；否则当 `ready_in` 时，把 `access_in & ~write_in` 打一拍——**只有读请求会生出响应**，写请求被屏蔽。

[emesh/hdl/emesh_readback.v:73-79](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_readback.v#L73-L79) — 同一拍、同一条件下：`datamode_out/ctrlmode_out` 直通，而 **`dstaddr_out <= srcaddr_in`**（地址回送：响应送往请求者）。

数据与高 32 位源地址直接取自 `read_data`：

[emesh/hdl/emesh_readback.v:81-82](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_readback.v#L81-L82) — `data_out = read_data[31:0]`（读数据贴进响应），`srcaddr_out = read_data[63:32]`。

> 这里 `read_data` 端口是 64 位，但 gpio 里 `read_data` 寄存器只声明成 N 位（见 4.3 节末尾），高半段未分配——又一个「以源码为准」的小漂移。

反压直接透传：

[emesh/hdl/emesh_readback.v:85](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_readback.v#L85) — `ready_out = ready_in`。

**(c) 重新打包成响应**

最后用打包器（实例名 `e2p` = emesh→packet）把响应字段压回 `packet_out`，并把 `write_out` 恒置 1（响应即「回写」）：

[emesh/hdl/emesh_readback.v:91-102](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_readback.v#L91-L102) — 调用 `enoc_pack`，把 `datamode_out/ctrlmode_out/dstaddr_out/data_out/srcaddr_out` 拼成 `packet_out`。

#### 4.3.4 代码实践：gpio 读 `GPIO_IN` 时，数据如何回到 `packet_out`

**目标**：把 unpack + readback 串起来，追踪一次「读 GPIO 输入寄存器」的完整数据通路。

**背景**：`GPIO_IN` 的地址索引是 `dstaddr[6:3]==4'h1`（见 [gpio/hdl/gpio_regmap.vh:5](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh#L5)）。读它得到的是各引脚的同步输入电平 `gpio_in_sync`。

**操作步骤**（源码跟踪）：

1. **收包解包**：读请求到达 gpio，先由解包器切片——[gpio/hdl/gpio.v:77-89](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L77-L89) 把 `packet_in` 拆成 `write_in/dstaddr_in/srcaddr_in/...`。因为是读，`write_in=0`。
2. **判定是读**：[gpio/hdl/gpio.v:92](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L92) `reg_read = access_in & ~write_in = 1`。
3. **取数据（gpio 自己预先把 read_data 备好）**：[gpio/hdl/gpio.v:213-223](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L213-L223) 在 `reg_read` 时按 `dstaddr_in[6:3]` 译码，命中 `GPIO_IN` 则把 `gpio_in_sync` 锁进 `read_data`。注意这一步发生在 gpio 里，**早于** readback——readback 只消费已经备好的 `read_data`。
4. **readback 装配响应**：[gpio/hdl/gpio.v:225-238](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L225-L238) 把 `access_in/packet_in/read_data/wait_in` 喂给 `emesh_readback`。readback 内部按 4.3.3 的流程：`dstaddr_out <= srcaddr_in`（送往请求者）、`data_out <= read_data[31:0]`（引脚电平）、`access_out` 晚一拍拉高，`packet_out` 重新打包且 `write_out=1`。

**需要观察的现象**：响应包 `packet_out` 里，目标地址变成了**请求者的回信地址**（而非原 `GPIO_IN` 地址），数据段是**当时 `gpio_in_sync` 的值**，控制位指示这是一次「写」（即读响应）。

**预期结果**：引脚电平经由 `gpio_in → oh_dsync → gpio_in_sync → read_data → emesh_readback.data_out → packet_out(数据字段)` 回到请求方。把这条链路画成时序图：第 0 拍 `access_in` 到达、`read_data` 在当拍锁存；第 1 拍 `access_out` 与 `packet_out` 同时出现（晚一拍）。**待本地验证**（仓库脚本路径有遗留问题，见 u1-l3）。

#### 4.3.5 小练习与答案

**练习 1**：readback 里 `access_out <= access_in & ~write_in`，为什么要把 `~write_in` AND 进去？

> **答**：readback 是**读响应**通路，只服务读事务。写事务不需要回数据，所以要在 readback 入口就把写吞掉，避免产生虚假的写响应包。

**练习 2**：如果不做 `dstaddr_out <= srcaddr_in` 这步地址回送，读响应会出什么问题？

> **答**：响应包会沿用原 `dstaddr`（外设寄存器地址）作为目标，于是响应被送回外设自己，而不是发起读的 CPU/主端，读数据永远到不了请求者。地址回送是把「请求里的回信地址」变成「响应里的目标地址」，这是任何「读需要回送」总线的共同范式。

**练习 3**：为什么 gpio 里要在调用 readback **之前**（213–223 行）自己先把 `read_data` 锁存好，而不是让 readback 去读寄存器？

> **答**：因为 readback 内部是固定的单级流水线，它在 `ready_in & access_in & ~write_in` 那一拍就要把数据打进 `data_out`。外设必须**提前一拍**把目标寄存器的值准备好送到 `read_data` 口上，时序才能对齐。这是一种「调用方负责取数、readback 负责打包」的清晰分工。

---

## 5. 综合实践

**任务**：在纸上完整演绎一次「读 `GPIO_ILAT`（中断锁存寄存器）」的事务，并把响应包的每个字段填出来。

1. 构造读请求包：自己任选一个请求者回信地址 `srcaddr`（如 `0x00000020`），目标地址指向 `GPIO_ILAT`（查 [gpio_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh) 得到其 `dstaddr[6:3]` 索引），`write=0`。
2. 追踪 gpio 内部：写一段话说明 `reg_read` 如何置 1、`read_data` 在哪一拍锁存了 `gpio_ilat` 的值（参考 [gpio.v:213-223](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L213-L223)）。
3. 追踪 readback：写出响应包 `packet_out` 中 `dstaddr/data/write` 三个字段的最终值，并解释为什么 `dstaddr` 不再是 `GPIO_ILAT` 的地址。
4. 进阶：假设这次是「写 `GPIO_OUTSET`」而非读，预测 `emesh_readback` 的 `access_out` 会是什么，并说明理由。

**交付物**：一张时序草图（`access_in` / `read_data` / `access_out` / `packet_out` 四行，标出先后拍）+ 一张响应包字段表。

> 本任务以源码阅读和推理为主，不强求仿真。若本地已按 u1-l3 修好脚本路径，可进一步用 `.emf`（u4-l2）发一条读 `GPIO_ILAT` 的事务，用 gtkwave 观察 `packet_out` 验证你的推导——但当前仓库脚本**不能开箱即跑**，标注**待本地验证**。

## 6. 本讲小结

- **pack/unpack** 是字段与扁平包之间的双向切片/拼接翻译器，靠 `generate` 适配多种 `AW/PW`；二者边界表必须严格对齐，且都体现「写数据与读回信地址共用同一片比特」。
- **emesh_decode** 把 16 位命令字译成 `cmd_write/cmd_read/size/length/...`，`write = ~opcode[3]`、`read = opcode==1000`。
- **wralign** 在写侧把 LSB 对齐的子字数据**复制广播**到所有字节通道；**rdalign** 在读侧按地址+宽度把目标子字**抽取**到低位——两者是方向相反的字节转向电路，存储型从机（`emesh_memory`）才需要，gpio 不用。
- **emesh_readback** 是读响应装配流水线：解包读请求 → `dstaddr_out <= srcaddr_in`（地址回送）→ `data_out <= read_data`（注入读数据）→ 重新打包（`write=1`）；写事务在入口被屏蔽。
- 外设（如 gpio）需**提前一拍**把寄存器值备到 `read_data`，readback 只负责按时序打包回送。
- 仓库存在接口迁移痕迹：`emesh_readback`/`gpio` 引用的 `enoc_unpack`/`enoc_pack`（旧 `packet2emesh` 接口）与现行 `emesh_pack`/`emesh_unpack`（`cmd_*` 接口）端口名不一致；`emesh_wralign` 当前无实例。一律以源码文本为准。

## 7. 下一步学习建议

- **进入第 6 单元（可配置外设）**：本讲的 readback + unpack 是 gpio 的回读骨架，下一讲 u6-l1（寄存器映射 `.vh` 模式）和 u6-l2（gpio 全解析）会把 gpio 的**写通路**（`GPIO_OUT/OUTSET/OUTCLR/OUTXOR` 的 mux4 位操作）和中断逻辑补全，建议紧接着读。
- **对照真实存储从机**：想看 pack/unpack/rdalign「能编译、被实例化」的完整范例，直接读 [emesh/hdl/emesh_memory.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_memory.v)，它是这三件原语的权威调用方。
- **向链路层延伸**：本讲处理的还是「片上」的包，u7 单元（elink）会讲这些包如何被打成帧、经 LVDS 串化送到另一颗芯片，届时可回看本讲，体会「字段→包→帧→比特」的逐层封装。
