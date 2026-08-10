# 双缓冲存储模型与 16 位像素打包

> 承接 [u3-l1](u3-l1-hdl-module-ports.md)：我们已经把 `image_processing` 当黑盒，理清了它对外只有「存储器接口」和「通信接口」两扇门。本讲走进那扇「存储器门」，看模块把 128KB 片上内存切成哪几块、怎么用，以及为什么像素要「两个一组」地打包。

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `MEMORY_SIZE`、`BUFFER_SIZE`、`BUFFER2_LOCATION` 三个参数把 128KB 切成两块 64KB 的来龙去脉，并能算出每块能装多少像素。
- 解释 `buffer_input_address` 和 `buffer_storage_address` 这两个地址寄存器的作用，以及 `COMMAND_SWITCH_BUFFERS` 为什么能「零成本」互换它们。
- 看懂 `STATE_SEND_IMG` 如何用 `memory_addr_counter[0]`（地址的最低位）把逐字节到来的像素「两两凑对」塞进一个 16 位存储字，并解释为什么必须凑满两个字节才置 `wr_en=1`。
- 理解 16 位打包带来的吞吐收益：读回和运算都按「字」走，一次处理 2 个像素。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自 u1、u2、u3-l1）：

- **存储字宽 16 位、每字装 2 个像素**：模块的 `data_read` / `data_write` 都是 16 位（见 [hdl/image_processing.v:17-22](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L17-L22)），而灰度像素只有 8 位（0~255）。所以一个 16 位字的高字节 `[15:8]` 和低字节 `[7:0]` 各放一个像素。
- **字节粒度地址、字粒度访问**：模块用一个 32 位寄存器 `memory_addr_counter` 当「字节地址」计数，它的最低位 `[0]` 就是「当前字节是偶数还是奇数」的指示——偶数对应低字节，奇数对应高字节。真正送给 RAM 的 `addr` 会把这一位清零，得到「字地址」。
- **单端口 RAM**：同一时刻只能读或只能写一个字，所以读 → 算 → 写回往往要拆成多个时钟周期（这点在 u4 会详细展开，本讲只在「打包收益」里点到）。
- **握手信号 `data_read_valid` / `comm_data_in_valid`**：u3-l1 已说明，本讲在用到处再复述。

> 一个直觉：你可以把 128KB 想成一条长条形的货架，被从中间锯成前后两段。前一段叫 input，后一段叫 storage。「互换缓冲」不是把货搬来搬去，而是把贴在两段上的「input」「storage」两块标签交换一下——东西还在原地。

## 3. 本讲源码地图

本讲只涉及一个源文件，但会反复在它的不同段落之间跳转：

| 段落 | 行号（HEAD `b1d7480`） | 作用 |
| --- | --- | --- |
| 存储容量参数 | [hdl/image_processing.v:78-83](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L78-L83) | 定义 128KB 总容量、两块 64KB 的边界 |
| 缓冲地址寄存器 | [hdl/image_processing.v:142-143](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L142-L143) | `buffer_input_address` / `buffer_storage_address` 两个基地址 |
| `initial` 初始化 | [hdl/image_processing.v:200-204](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L200-L204) | 上电时两个地址都先设成 0（真正的初始化留给 `COMMAND_PARAM`） |
| `COMMAND_PARAM` 初始化 | [hdl/image_processing.v:225-230](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L225-L230) | 把 input 设为 0、storage 设为 `BUFFER2_LOCATION` |
| `COMMAND_SWITCH_BUFFERS` | [hdl/image_processing.v:253-257](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L253-L257) | 两个地址寄存器互换（3 行代码完成交换） |
| `COMMAND_SEND_IMG` 入口 | [hdl/image_processing.v:231-235](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L231-L235) | 用 `buffer_input_address` 作为写入起点 |
| `STATE_SEND_IMG` 装配 | [hdl/image_processing.v:334-353](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L334-L353) | 偶/奇字节装配成 16 位字的核心循环 |
| `STATE_READ_IMG` 拆字 | [hdl/image_processing.v:373-401](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L373-L401) | 读回时把 16 位字拆成两个字节送出（对称过程） |
| `STATE_PROC_UNARY` 写回 | [hdl/image_processing.v:549-551](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L549-L551) | 运算时一次写回 2 个像素（打包的收益） |

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. 存储容量参数：128KB 怎么切成两个 64KB。
2. 双缓冲地址寄存器与 `COMMAND_SWITCH_BUFFERS` 零成本互换。
3. 16 位打包写：`STATE_SEND_IMG` 的偶/奇字节装配。
4. 打包的收益：回读拆字与运算一次处理 2 像素。

### 4.1 存储容量参数：128KB 怎么切成两个 64KB

#### 4.1.1 概念说明

iCE40 UltraPlus 的片上 SPRAM 总共给本模块 **128KB**。项目把这 128KB 从中间一分为二，得到两块各 **64KB** 的缓冲：

- **input 缓冲**：图像「进来」和「读出」都走这里。
- **storage 缓冲**：所有运算（加减、阈值、卷积……）都在这里进行，结果也写回这里。

为什么是 64KB 一块？因为 64KB 正好能装下一幅 \(256 \times 256\) 的单通道灰度图：

\[
256 \times 256 \times 1\,\text{B} = 65536\,\text{B} = 64\,\text{KB}
\]

项目里的测试图也是这个量级（见 [hdl/image_processing.v:78-80](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L78-L80) 的注释 `256*256 images assuming 1B/pixel`）。所以「两块 64KB」=「同时能放下两整幅图」，这正是双图运算（binary add/sub/mult）所需要的：一幅放 input、一幅放 storage。

#### 4.1.2 核心流程

128KB 的地址空间（字节粒度，范围 `0 ~ 131071`）被逻辑上分成连续的两段：

```
地址 (字节)
0 ─────────────────────── 65535 │ 65536 ─────────────────── 131071
└──── 第一块 64KB ────────────┘ └──── 第二块 64KB ───────────┘
        （某时刻叫 input）              （某时刻叫 storage）
```

要点：

- 这「两块」是**同一段连续物理内存上的两个地址区间**，不是两块独立的 RAM 芯片。
- 哪一段叫 input、哪一段叫 storage，由两个地址寄存器决定（下一节讲）。
- 第二块的起始字节地址就是 `MEMORY_SIZE/2 = 65536`，这就是 `BUFFER2_LOCATION` 的含义。

> 注意单位：`BUFFER2_LOCATION` 是**字节地址**，不是字地址。因为模块对外用字节粒度计数（`memory_addr_counter`），位 `[0]` 用来区分字内高低字节（4.3 节会看到）。

#### 4.1.3 源码精读

三个参数定义在 [hdl/image_processing.v:78-83](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L78-L83)：

```verilog
//128KB memory available on spram
//2*64KB
//256*256 images assuming 1B/pixel
parameter MEMORY_SIZE = 1024*128;          // 131072 字节 = 128KB
parameter BUFFER_SIZE = MEMORY_SIZE/2;     // 65536  字节 = 64KB（单块容量）
parameter BUFFER2_LOCATION = MEMORY_SIZE/2;// 65536  字节 = 第二块的起始地址
```

三个常量其实只用到了两个不同的数值：`BUFFER_SIZE`（每块多大）和 `BUFFER2_LOCATION`（第二块从哪开始），它们恰好都等于 `MEMORY_SIZE/2`。作者分别起名是为了「语义清晰」——一个描述容量、一个描述位置。

> 细节：代码里 `BUFFER_SIZE` 这个参数在本模块中后续并没有被直接引用，真正参与寻址的是 `BUFFER2_LOCATION`。它更多是一份「设计文档式」的声明，告诉读者「每块就是这么大」。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认两块缓冲的容量与边界数值。
2. **步骤**：打开 [hdl/image_processing.v:78-83](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L78-L83)，按计算器算 `1024*128`、`MEMORY_SIZE/2`。
3. **观察**：手算 \(256 \times 256 = 65536\)，正好等于 `BUFFER2_LOCATION`。
4. **预期结果**：你会得到「第一块占字节 0~65535，第二块占字节 65536~131071，每块恰好能装一幅 256×256 图」的结论。

#### 4.1.5 小练习与答案

**练习 1**：如果改用 \(200 \times 200\) 的图（仍是 1 字节/像素），一块 64KB 还装得下吗？

> 答案：\(200 \times 200 = 40000\) 字节 ≈ 39KB < 64KB，装得下，而且绰绰有余。但要注意：模块里的卷积行缓冲、地址计数器都是按 256 宽度上限设计的（如 `CONVOLUTION_LINE_MAX_SIZE = 256`），所以「装得下」不等于「任意尺寸都能跑」。

**练习 2**：`BUFFER2_LOCATION` 为什么是 `MEMORY_SIZE/2` 而不是 `MEMORY_SIZE`？

> 答案：因为它是**第二块的起始地址**，即第一块结束之后的位置。第一块占了 `0 ~ MEMORY_SIZE/2 - 1`，所以第二块从 `MEMORY_SIZE/2` 开始。如果写成 `MEMORY_SIZE` 就越界了。

---

### 4.2 双缓冲地址寄存器与 COMMAND_SWITCH_BUFFERS 零成本互换

#### 4.2.1 概念说明

上一节说「哪一段叫 input、哪一段叫 storage 由地址寄存器决定」。这两个寄存器就是：

- `buffer_input_address`：input 缓冲的基地址。
- `buffer_storage_address`：storage 缓冲的基地址。

它们不是固定常量，而是**可改写的寄存器**。正因为可改写，项目才能用一条命令 `COMMAND_SWITCH_BUFFERS` 把两个寄存器的值对调——这就是「双缓冲切换」。

为什么需要切换？因为图像处理往往是**一连串操作**：先加载图 → 做反色 → 再做卷积 → 读出结果。如果每次操作都要把数据从一块内存搬到另一块，会非常浪费带宽。双缓冲 + 切换的妙处在于：**只换标签，不搬数据**。结果留在原地，只是它从「storage 角色」变成了「input 角色」，下一轮操作可以接着用。

此外，双图运算（binary）天然需要同时访问两幅图——一幅在 input、一幅在 storage——这只有两块独立缓冲才能做到（见 [README.md:18-20](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L18-L20) 的说明）。

#### 4.2.2 核心流程

一次典型 unary 运算（例如反色）的缓冲状态变化（结合 u1-l5 的「三明治」调用序列）：

```
① COMMAND_PARAM         input=0,      storage=65536   （初始化：空图区与图区分开）
② COMMAND_SEND_IMG      → 把图像字节写入 input=0 这一段
                        input=0,      storage=65536   （图现在在地址 0~65535）
③ COMMAND_SWITCH_BUFFERS（第 1 次切换）
                        input=65536,  storage=0       （图所在段变成了 storage！）
④ 运算（反色）          读 storage=0，写回 storage=0  （原地处理，结果在地址 0）
⑤ COMMAND_SWITCH_BUFFERS（第 2 次切换）
                        input=0,      storage=65536   （结果所在段又变回 input）
⑥ COMMAND_READ_IMG      从 input=0 读出 → 主机拿到结果
```

可以看到 `read_image` 永远从 `buffer_input_address` 读，运算永远在 `buffer_storage_address` 上进行；切换就是把结果「挪进可读的 input 槽位」的手段。整个过程没有任何像素被复制。

`COMMAND_SWITCH_BUFFERS` 的互换本身只用一个时钟周期：它只改两个寄存器，不动任何存储数据，所以叫「零成本」。

#### 4.2.3 源码精读

两个地址寄存器声明在 [hdl/image_processing.v:142-143](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L142-L143)，都是 32 位（与 `addr` 端口等宽）：

```verilog
reg [31:0] buffer_input_address;
reg [31:0] buffer_storage_address;
```

**初始化（两步）**。`initial` 块里两者都被设为 0，但注释明确写了 storage 的真正初值要在 `COMMAND_PARAM` 里给（[hdl/image_processing.v:200-204](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L200-L204)）：

```verilog
buffer_input_address = 0;
//doesn't want to be init at this value ==> do it in init state
// buffer_storage_address = BUFFER2_LOCATION;
buffer_storage_address = 0;
```

> 这段注释「doesn't want to be init at this value」的意思是：在 FPGA 上电 / Verilator 构造时的 `initial` 阶段，作者发现把 storage 直接设成 `BUFFER2_LOCATION` 不可靠（SPRAM 初值行为与仿真器不同），于是把它留到主机发 `COMMAND_PARAM` 命令时再统一初始化。这就是「真正的初始化交给命令」的设计。

真正的初始化发生在 `COMMAND_PARAM` 分支（[hdl/image_processing.v:225-230](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L225-L230)）：

```verilog
COMMAND_PARAM: begin //also acts as init
   state <= STATE_READ_COMMAND_PARAM_WIDTH;
   counter_read <= 1; //will be used to read the 16bits
   buffer_storage_address <= BUFFER2_LOCATION;   // storage 指向第二块
   buffer_input_address <= 0;                    // input  指向第一块
end
```

所以 `COMMAND_PARAM` 不只是「发送图像宽高」，它还兼任「把双缓冲复位到默认布局」。注释 `//also acts as init` 就是这个意思。

**互换**。`COMMAND_SWITCH_BUFFERS` 只用 3 行（[hdl/image_processing.v:253-257](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L253-L257)）：

```verilog
COMMAND_SWITCH_BUFFERS: begin
   state <= STATE_WAIT_COMMAND;
   buffer_input_address <= buffer_storage_address;   // 新 input  ← 旧 storage
   buffer_storage_address <= buffer_input_address;   // 新 storage ← 旧 input
end
```

这两行用的是 **Verilog 非阻塞赋值 `<=`**，是「零成本互换」能成立的关键。非阻塞赋值的特点是：所有右值都取**进入这个时钟沿之前**的旧值，然后再统一更新。因此这两行在同一个时钟里「同时」读旧值、同时写新值，效果等价于用了一个临时变量做交换，却不需要临时变量。若误写成阻塞赋值 `=`，第二行就会读到第一行刚写的新值，导致两个寄存器都变成同一个数——经典陷阱。

**消费这两个地址的地方**：

- 发图：`COMMAND_SEND_IMG` 把 `memory_addr_counter` 设为 `buffer_input_address`（[hdl/image_processing.v:231-235](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L231-L235)），即图像总是写入 input。
- unary 运算：把 `proc_memory_addr_counter` 设为 `buffer_storage_address`（如 [hdl/image_processing.v:268](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L268) 的反色），即运算总在 storage 上。
- binary 运算：同时用 `buffer_storage_address` 和 `buffer_input_address` 做偏移寻址（[hdl/image_processing.v:567](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L567) 与 [hdl/image_processing.v:574](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L574)），分别读两幅图。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：解释 `COMMAND_PARAM` 为什么把 `buffer_storage_address` 设为 `BUFFER2_LOCATION`、`buffer_input_address` 设为 0。
2. **步骤**：对照 [hdl/image_processing.v:225-230](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L225-L230) 与 4.2.2 的流程图，跟踪 `send_image`、两次 `switch_buffers`、`read_image` 各自用到哪个地址。
3. **思考点**：为什么让 input 从 0 开始、storage 从 65536 开始，而不是反过来？
4. **预期结果**：你应该能得出这样的解释——发图先写 input(0)；切一次让图所在段变成 storage 以便运算；运算完再切一次让结果段变回 input 以便 `read_image` 读取。如果一开始就把图写进 storage 那段，unary 运算虽然也能就地处理，但 `read_image` 固定从 input 读，就得多绕一次切换。把 input 放在地址 0 也让 `memory_addr_counter` 的初值更直观。

#### 4.2.5 小练习与答案

**练习 1**：把 `COMMAND_SWITCH_BUFFERS` 里的两行非阻塞赋值 `<=` 改成阻塞赋值 `=`，会发生什么？

> 答案：阻塞赋值按顺序执行——第一行先把 `buffer_input_address` 改成了旧 `buffer_storage_address` 的值，第二行再去读 `buffer_input_address` 时拿到的已经是新值，于是 `buffer_storage_address` 也被赋成同一个值。结果是两个寄存器相同，缓冲「身份」丢失。这正是非阻塞赋值对「同周期交换」必不可少的根本原因。

**练习 2**：连续发两次 `COMMAND_SWITCH_BUFFERS`，缓冲布局会变成什么？

> 答案：变回原样。两次互换等于没换（对合性）。所以「多切一次」是安全的纠错手段，但也会让 input/storage 角色错位，调用方必须成对使用。

---

### 4.3 16 位打包写：STATE_SEND_IMG 的偶/奇字节装配

#### 4.3.1 概念说明

主机送图时，像素是**一个字节一个字节**地从通信接口 `comm_data_in` 到来的（见 u2-l3：最终 `image_input[]` 是一个一维字节灰度数组，逐字节发送）。但存储器的最小访问单位是 **16 位字**——`data_write` 是 16 位的（[hdl/image_processing.v:17-22](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L17-L22)）。

于是需要一个「装配」过程：把**每两个连续的字节**拼成一个 16 位字，第一个字节放低 8 位 `[7:0]`，第二个字节放高 8 位 `[15:8]`，凑满两个字才发起一次写。

为什么要打包？因为同一个存储器访问周期里写 16 位比写 8 位「划算一倍」——吞吐翻番，而控制逻辑几乎不复杂。这是资源受限 FPGA 上很常见的节流手法。

#### 4.3.2 核心流程

`STATE_SEND_IMG` 用一个「字节地址计数器」`memory_addr_counter` 来追踪当前写到第几个字节，靠它的**最低位 `[0]`** 区分两个阶段：

```
进入 STATE_SEND_IMG 前：memory_addr_counter <= buffer_input_address（偶数）
                       counter_read          <= img_width*img_height

每个 comm_data_in_valid=1 的周期，吞入一个字节：

  memory_addr_counter[0] == 0（偶）：
     data_write[7:0]  <= comm_data_in      ← 存住低字节，先不写
     memory_addr_counter + 1               ← 计数器变奇

  memory_addr_counter[0] == 1（奇）：
     data_write[15:8] <= comm_data_in      ← 补上高字节，现在 16 位凑齐
     wr_en <= 1                             ← 这才发起写
     addr  <= {memory_addr_counter[31:1], 1'b0}  ← 字地址（位0清零）
     memory_addr_counter + 1               ← 计数器变偶，准备下一对

  每吞一字节：counter_read - 1；减到 0 时回到 STATE_WAIT_COMMAND。
```

要点：

- 偶数拍**只存不写**，奇数拍**才写**。所以「两个字节凑齐才置 `wr_en=1`」。
- `addr` 用 `{memory_addr_counter[31:1], 1'b0}`，本质是把字节地址的最低位清零，得到**字地址**。每写一次，`memory_addr_counter` 跨越 2 个字节，`addr` 跨越 1 个字。
- 因此 `counter_read` 初始值 `img_width*img_height` 必须是**偶数**（图像像素数为偶），否则最后一个奇字节没有伙伴、无法凑成字。代码里也多处注释 `image size mod 2 should be 0`（见 [hdl/image_processing.v:379](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L379)）。

#### 4.3.3 源码精读

入口设置在 [hdl/image_processing.v:231-235](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L231-L235)，把起点设为 input 缓冲基址、计数器设为总像素数：

```verilog
COMMAND_SEND_IMG: begin
   state <= STATE_SEND_IMG;
   counter_read <= img_width*img_height;
   memory_addr_counter <= buffer_input_address;   // 从 input 段开头开始写
end
```

装配循环在 [hdl/image_processing.v:334-353](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L334-L353)：

```verilog
STATE_SEND_IMG: begin //receives image from the host
   if(comm_data_in_valid == 1) begin

      if(memory_addr_counter[0] == 1'b0) begin
         data_write[7:0] <= comm_data_in;          // 偶：存低字节
      end else begin
         data_write[15:8] <= comm_data_in;         // 奇：存高字节
         wr_en <= 1;                               //   凑齐，发起写
         addr <= {memory_addr_counter[31:1], 1'b0};//   字地址（位0清零）
      end

      memory_addr_counter <= memory_addr_counter+1;// 字节计数器恒+1

      if(counter_read > 1) begin
         counter_read <= counter_read - 1;
      end else begin
         state <= STATE_WAIT_COMMAND;              // 全部像素收完
      end
   end
end
```

两个细节值得强调：

1. **`memory_addr_counter` 每拍都 `+1`**（无论偶奇），但 `wr_en` 只在奇数拍置 1。所以「每两拍写一次」，写地址每两拍才更新——这正好对应「2 字节 → 1 字」。
2. **`addr` 的最低位被强制为 0**。因为一次写必须是完整的 16 位字，不能只写半个字。位 `[0]` 在这里失去了「高低字节选择」的意义，被清零后剩下的高位就是字地址。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：追踪 `STATE_SEND_IMG` 的偶/奇装配，解释「为什么必须两个字节凑齐才置 `wr_en=1`」。
2. **步骤**：
   - 假设一幅 \(4 \times 2 = 8\) 像素的小图，像素值依次是 `10, 20, 30, 40, 50, 60, 70, 80`。
   - `buffer_input_address = 0`，所以 `memory_addr_counter` 从 0 开始。
   - 列表跟踪前 8 个 `comm_data_in_valid` 周期里 `memory_addr_counter`、`memory_addr_counter[0]`、`data_write`、`wr_en`、`addr` 的取值。
3. **观察并思考**：
   - 第 0 拍（计数器 0，偶）：`data_write[7:0]=10`，`wr_en=0`，没写。
   - 第 1 拍（计数器 1，奇）：`data_write[15:8]=20`，`wr_en=1`，`addr=0`，把 `0x140A`（高字节 20、低字节 10）写入字地址 0。
   - 第 2、3 拍：写入字地址 2，内容 `0x2828`……（高 40 低 30）。
   - 注意 `addr` 步进是 **2**（字节），即每字 1 步。
4. **预期结论**：`wr_en=1` 只在奇数拍出现，是因为偶数拍只拿到了「半字」（低字节），单独写一个 16 位存储字会丢失或污染高字节；必须等到奇数拍补上高字节，16 位才完整。这就是「凑齐两字节才写」的根本原因。
5. **待本地验证**：若你本地装好了 Verilator（见 [u1-l3](u1-l3-build-run-simulation.md)），可在 `simulation/image_processing_simulation.cpp` 的 `memory[]` 写入处加一行打印，观察每两次 `wr_en==1` 才出现一次写入、且写入值的高低字节正好是相邻两个像素。

#### 4.3.5 小练习与答案

**练习 1**：如果图像像素总数是奇数（例如 \(3 \times 3 = 9\)），`STATE_SEND_IMG` 会在最后一个字节发生什么？

> 答案：前 8 个字节正常凑成 4 个字写入。第 9 个字节到来时 `memory_addr_counter` 为偶（8），它被存进 `data_write[7:0]`，但永远等不到「奇数拍」来补高字节——因为 `counter_read` 已减到 0，状态直接跳回 `STATE_WAIT_COMMAND`，`wr_en` 从未对该字节置 1。最后一个像素被丢弃。所以项目要求图像尺寸为偶数。

**练习 2**：`addr <= {memory_addr_counter[31:1], 1'b0}` 和直接写 `addr <= memory_addr_counter` 在「奇数拍」时有什么差别？

> 答案：奇数拍时 `memory_addr_counter` 的最低位是 1。直接赋值会让 `addr` 末位为 1，但存储器以字为单位、地址末位应为 0，这属于「未定义 / 浪费的位」。用拼接把末位强制清零，等价于 `memory_addr_counter & ~1`，保证 `addr` 始终是合法的字地址。

---

### 4.4 打包的收益：回读拆字与运算一次处理 2 像素

#### 4.4.1 概念说明

16 位打包不只是「写入」时省事，它让**整条数据通路**都按「字」走，从而处处省一半周期：

- **读回**（`STATE_READ_IMG`）：从存储器一次读出一个 16 位字，再拆成两个字节依次经 `comm_data_out` 送回主机。
- **运算**（如 `STATE_PROC_UNARY`）：一次从存储器读出一个 16 位字 = 2 个像素，对高低字节**并行**做同样的运算，再一次性写回。

这就是「2 像素打包」的真正动机：在不增加存储器位宽成本的前提下，把吞吐翻倍。本节只展示这个收益的「形」，详细的回读状态机在 [u3-l4](u3-l4-send-receive-states.md)，详细的运算状态机在 u4。

#### 4.4.2 核心流程

**回读拆字**（`STATE_READ_IMG`，与 4.3 完全对称）：

```
偶数拍：rd_en<=1, addr<=memory_addr_counter，发起一次字读，计数器+1
奇数拍：等 data_read_valid，把 16 位 data_read 存进 mem_data_buffer；
        然后分两步把低/高字节经 comm_data_out 送出。
```

这里用 `counter_read[0]`（剩余像素数的奇偶性）来选择当前该送低字节还是高字节：偶送低、奇送高。送完高字节才让 `memory_addr_counter+1` 去读下一个字。注意：读地址只在「输出高字节」时才前进——因为一个 16 位字要喂出两个字节，读一次够用两次。

**运算写回**（`STATE_PROC_UNARY`）：以反色为例，读出一个字后，`data_write <= ~data_read` 一条语句同时对高低两个像素求反（[hdl/image_processing.v:538-539](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L538-L539)），然后一次写回。所以每「读一次 → 算一次 → 写一次」处理 **2 个像素**，效率比逐像素翻倍。

#### 4.4.3 源码精读

回读状态 [hdl/image_processing.v:373-401](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L373-L401)（只看关键骨架）：

```verilog
STATE_READ_IMG: begin
   if(memory_addr_counter[0] == 1'b0) begin
      rd_en <= 1;
      addr <= memory_addr_counter;            // 偶：发起字读
      memory_addr_counter <= memory_addr_counter + 1;
   end else begin
      if (counter_read[0] == 1'b0 && data_read_valid == 1'b1) begin
         mem_data_buffer <= data_read;        // 奇：先把整字存进缓冲
         mem_data_buffer_full <= 1;
      end
      if( comm_data_out_free == 1 && mem_data_buffer_full == 1 ) begin
         if (counter_read[0] == 1'b0) begin
            comm_data_out <= mem_data_buffer[7:0];   // 先送低字节
            ...
         end else begin
            comm_data_out <= mem_data_buffer[15:8];  // 再送高字节
            memory_addr_counter <= memory_addr_counter+1; // 送完高字节才读下一字
            ...
         end
      end
   end
end
```

运算写回 [hdl/image_processing.v:549-551](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L549-L551)（unary 处理完一整个字后）：

```verilog
//write back the data
wr_en <= 1;
//16bits data addressing
addr <= {proc_memory_addr_counter[31:1], 1'b0};   // 字地址
proc_memory_addr_counter <= proc_memory_addr_counter+1;
```

注意 `proc_counter_read` 每次减 **2**（见 [hdl/image_processing.v:553-554](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L553-L554)），正是「一次处理 2 像素」的体现。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：看清「读回时地址只在送高字节时才前进」这一设计。
2. **步骤**：对照 [hdl/image_processing.v:384-398](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L384-L398)，跟踪 `memory_addr_counter+1` 这条赋值出现在哪个分支。
3. **观察**：它只出现在 `counter_read[0]==1`（送高字节）分支里。
4. **预期结论**：一个 16 位字要拆成两次 `comm_data_out` 输出，所以读地址每两个字节奏进一次。这与 4.3「每两个字节写一次」是对偶关系——写入是「2 进 1」，读出是「1 出 2」。
5. **待本地验证**：在仿真器的 `comm_data_out` 侧记录输出序列，应看到像素顺序与发送时完全一致（低字节先出）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `STATE_PROC_UNARY` 里 `proc_counter_read` 每次减 2，而不是减 1？

> 答案：因为一次循环处理的是一整个 16 位字，即 2 个像素。计数器以「像素」为单位计数（初值 `img_width*img_height`），所以每次要减 2。

**练习 2**：回读时为什么用 `counter_read[0]` 选择低/高字节，而不是像写入那样用 `memory_addr_counter[0]`？

> 答案：读回时 `memory_addr_counter` 的步进节奏与「输出第几个字节」并不对齐（它在偶数拍发起读、在送高字节时才 +1），用它来区分高低字节会错位。而 `counter_read` 从总像素数（偶数）开始递减，其奇偶性与「当前是第几个输出字节」严格对应：偶数对应低字节、奇数对应高字节。所以这里改用 `counter_read[0]` 作为字节选择信号。

---

## 5. 综合实践

**任务**：画一张「双缓冲地址变迁表」，把一次完整的 unary 运算串起来，并验证 16 位打包的地址步进。

1. **准备**：选 `test_add_threshold` 或 `COMMAND_APPLY_INVERT` 作为目标运算（见 [u1-l5](u1-l5-host-main-flow.md) 的三明治序列）。
2. **画地址变迁表**：列出每个命令节点处 `buffer_input_address`、`buffer_storage_address` 的值（参考 4.2.2 的流程图）。
3. **叠加字节流**：在 `COMMAND_SEND_IMG` 那一行，画出前 4 个像素字节如何两两凑成 2 个 16 位字、写入哪两个 `addr`（字地址）。
4. **关键提问**：
   - 如果在 `send_image` 之前忘了发 `COMMAND_PARAM`，`buffer_storage_address` 会是多少？（提示：看 [hdl/image_processing.v:200-204](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L200-L204) 的 `initial` 值。）
   - 如果把 `COMMAND_SWITCH_BUFFERS` 的两行写成阻塞赋值，4.2.2 流程会在哪一步崩溃？
5. **预期结果**：你能用一张表完整解释「为什么图像进 input、运算在 storage、读出又从 input」这套零拷贝流程，并说清 16 位打包让地址步进变为「每两字节一字」。

> 进阶（可选，需本地 Verilator 环境）：在 `simulation/image_processing_simulation.cpp` 模拟存储器写回处加日志，打印每次 `wr_en==1` 时的 `addr` 与 `data_write`，验证相邻两个像素被合并进同一个 16 位字。

## 6. 本讲小结

- 128KB 片上内存被参数 `MEMORY_SIZE`、`BUFFER_SIZE`、`BUFFER2_LOCATION` 切成两块各 64KB，每块正好装一幅 \(256 \times 256\) 单通道灰度图。
- `buffer_input_address` 与 `buffer_storage_address` 是两个可改写的基地址寄存器；图像进 input、运算在 storage、读出又从 input。
- `COMMAND_SWITCH_BUFFERS` 用两条非阻塞赋值在一个时钟里完成互换，**只换标签不搬数据**，是零拷贝链式运算的关键。
- `COMMAND_PARAM` 兼任初始化：把 input 设为 0、storage 设为 `BUFFER2_LOCATION`，因为 `initial` 阶段设 storage 不可靠。
- 主机逐字节送来的像素在 `STATE_SEND_IMG` 里被「两两凑对」塞进 16 位字，靠 `memory_addr_counter[0]` 区分偶/奇，**只有凑满两字节才置 `wr_en=1`**。
- 打包让整条通路按「字」走：写入「2 进 1」、读出「1 出 2」、运算一次处理 2 像素，吞吐翻倍。前提是图像像素数为偶。

## 7. 下一步学习建议

- 想看回读状态机的完整时序（`mem_data_buffer` 缓冲、`counter_read[0]` 字节选择、`busy` 位）→ 继续读 [u3-l4 图像发送/接收与参数读取状态](u3-l4-send-receive-states.md)。
- 想看「读 → 算 → 写回」如何拆成多个时钟周期、`processing_command` 如何选择运算 → 进入 u4 单元，先读 [u4-l1 逐像素运算：STATE_PROC_UNARY](u4-l1-unary-operations.md)。
- 想从全局看清一次完整运算的字节级报文 → 回顾 [u2-l2 命令协议与报文格式](u2-l2-command-protocol.md)，对照本讲的地址模型理解「参数字节 vs 图像字节」的不同编码。
