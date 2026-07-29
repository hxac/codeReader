# 内存控制器：带宽节流

## 1. 本讲目标

上一讲 u3-l1 我们站在「GPU 边界」看了外部内存这一侧：内存长什么样、有几条通道、用什么信号握手。本讲往里走一步，拆开边界上那两个 `controller` 实例的**内部**——看它为什么存在、靠什么机制在「众多消费者」和「有限通道」之间搬运请求。

学完本讲，你应当能够：

- 说出 `controller` 存在的根本原因：消费者（LSU/fetcher）比外部内存通道多，产生「带宽瓶颈」，需要一个仲裁/中继者。
- 画出每条通道独立的「五态状态机」`IDLE / READ_WAITING / WRITE_WAITING / READ_RELAYING / WRITE_RELAYING`，并说清每个状态进入与退出的条件。
- 解释 `for` 循环里那个 `break` 的作用：**一条通道一个周期只拾取一个请求**。
- 解释 `channel_serving_consumer` 这一位寄存器为什么用**阻塞赋值** `=` 而其余赋值用**非阻塞** `<=`——它是防止多条通道在同一周期「重复拾取同一个消费者」的去重锁。
- 对照 LSU 的握手，描述一次 data 内存读的完整中继时序（请求→等待外部应答→把数据扔回消费者→等消费者确认→回 IDLE）。

本讲**不**深入 LSU/fetcher 的内部状态机（那是 u5-l3 和 u4-l3 的内容），也**不**讨论缓存、合并等优化（那是 u7-l2 的内容）；我们只聚焦 `controller.sv` 这一个文件。

## 2. 前置知识

开始前请确认你对以下直觉已有把握（不熟可回看 u2-l1「gpu.sv 顶层架构」与 u3-l1「内存模型与外部接口」）：

- **valid/ready 握手**：请求方拉 `valid`，应答方拉 `ready`，同时为高才完成一次事务。本讲里，**消费者**（LSU/fetcher）是请求方，**外部内存**是应答方，而 `controller` 夹在中间，对消费者扮演「应答方」、对外部内存扮演「请求方」。
- **打包线与未打包数组**：`[NUM_CHANNELS-1:0] x` 是每位代表一条通道的打包线；`[W-1:0] x [N-1:0]` 是 N 条独立的 W 位线。本讲里 `channel_serving_consumer` 是「每位对应一个消费者」的打包线，`controller_state[i]` 是「每条通道一份状态」的未打包数组。
- **阻塞 `=` 与非阻塞 `<=`**：在 `always @(posedge clk)` 里，非阻塞赋值在**当前周期末尾**统一更新、要到**下一周期**才被读到；阻塞赋值**立刻**生效，同一周期后面的语句就能读到新值。这一区别是本讲 `channel_serving_consumer` 去重机制的关键，务必先建立这个直觉。
- **仲裁（arbitration）**：当多个请求者抢同一个有限资源时，需要一个「裁判」决定谁先谁后。`controller` 就是消费者与内存通道之间的裁判。

> 关键认知：`controller.sv` 顶部的注释已经把它的职责说得很清楚——「接收所有核的内存请求、根据有限的外部带宽做节流、等待外部响应再分发回各核」（[src/controller.sv:4-7](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L4-L7)）。本讲就是把这三句话逐行拆开。

## 3. 本讲源码地图

本讲的核心只有一个文件，另两个文件用于把控制器「放到系统里」与「接到消费者」：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [src/controller.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv) | 内存控制器本体 | 全部机制都在这里：参数、五态状态机、去重锁、中继时序 |
| [src/gpu.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv) | GPU 顶层 | 两个 controller 实例如何被实例化、参数怎么传（说明 N 与 M 各是多少） |
| [src/lsu.sv](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv) | 消费者一侧 | LSU 如何拉高 `read_valid`、如何看到 `read_ready` 后收下数据（中继的「对手方」） |

数据流上，控制器处在「N 个消费者 ↔ M 条外部通道」的夹层：

```
   consumer 0 ─┐
   consumer 1 ─┤                    ┌── mem channel 0 ──┐
   consumer 2 ─┼──> [ controller ] ─┼── mem channel 1 ──┼──> 外部内存
      ...      ┤   (N 进 M 出仲裁)   ├── mem channel 2 ──┤
 consumer N-1 ─┘                    └── mem channel M-1 ─┘
   (LSU/fetcher)                      (data_mem/program_mem 端口)
```

## 4. 核心概念与源码讲解

### 4.1 NUM_CONSUMERS vs NUM_CHANNELS：带宽瓶颈从何而来

#### 4.1.1 概念说明

想象一个窗口：窗口外排着 N 个想办事的人（消费者），但窗口里只有 M 个办事员（内存通道），而且通常 \( N > M \)。如果不加管理，N 个人同时挤进来，办事员根本处理不过来。`controller` 就是这个窗口的「叫号机」：它把 N 路请求**仲裁**到 M 条通道上，让有限的带宽被有序使用。

这就是「节流（throttle）」的字面含义——**故意**把请求放慢到通道能承受的程度，避免拥塞。

#### 4.1.2 核心流程

控制器的两个规模参数决定了「拥塞程度」：

- `NUM_CONSUMERS`：通过本控制器访问内存的消费者数量。
- `NUM_CHANNELS`：能并发送达外部内存的通道数量。

「拥塞比」可以简单记为：

\[
\text{过订阅率} = \frac{\text{NUM\_CONSUMERS}}{\text{NUM\_CHANNELS}}
\]

过订阅率越高，同一时刻「抢不到通道」的消费者就越多，平均等待周期越长。

在 tiny-gpu 的两个实例里，这两个数字是不对称的（由顶层实例化时传入，见 [src/gpu.sv:85-134](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L85-L134)）：

| 控制器 | NUM_CONSUMERS | NUM_CHANNELS | 过订阅率 | 消费者是谁 |
| --- | --- | --- | --- | --- |
| data 内存控制器 | 8（`NUM_LSUS = 2 核 × 4 线程`） | 4 | 2 | 每个线程的 LSU |
| program 内存控制器 | 2（`NUM_FETCHERS = 2 核`） | 1 | 2 | 每个核的 fetcher |

两个控制器都恰好 2 倍过订阅。注意程序内存虽然只有 1 条通道，但它的消费者也只有 2 个 fetcher，所以拥塞并不比 data 内存更严重。

#### 4.1.3 源码精读

控制器把规模参数显式声明为 `parameter`，并配了注释说明含义：

[src/controller.sv:8-13](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L8-L13) —— 四个核心参数：`NUM_CONSUMERS`（消费者数）、`NUM_CHANNELS`（并发通道数）、`WRITE_ENABLE`（本控制器是否允许写）。注释明确写出「throttle requests based on limited external memory bandwidth」。

这两个参数在顶层被实例化时填上具体数字。data 控制器把 `NUM_CONSUMERS` 接到 `NUM_LSUS`：

[src/gpu.sv:85-91](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L85-L91) —— data 内存控制器实例：`NUM_CONSUMERS=NUM_LSUS`（=8）、`NUM_CHANNELS=DATA_MEM_NUM_CHANNELS`（=4）。8 个 LSU 抢 4 条通道。

program 控制器则把消费者设为 fetcher，通道只有 1 条，并关掉写：

[src/gpu.sv:115-121](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L115-L121) —— program 内存控制器实例：`NUM_CONSUMERS=NUM_FETCHERS`（=2）、`NUM_CHANNELS=1`、`WRITE_ENABLE(0)`。2 个 fetcher 抢 1 条通道。

> 旁注：`WRITE_ENABLE` 在本文件里只是「声明了意图」的文档型参数——在 `always` 块的正文里并没有出现对它的引用。program 内存之所以真正「只读」，是因为它的 fetcher 永远不会拉高 `consumer_write_valid`，且顶层实例化时根本没接 `mem_write_*` 端口（[src/gpu.sv:121-134](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L121-L134)）。本讲后续以 data 控制器（可读写）为主来讲解。

#### 4.1.4 代码实践

1. **实践目标**：亲手把两个控制器的「过订阅率」算出来，建立「N 进 M 出」的直觉。
2. **操作步骤**：
   - 在 [src/gpu.sv:58](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L58) 找到 `localparam NUM_LSUS = NUM_CORES * THREADS_PER_BLOCK`，代入默认值 `NUM_CORES=2, THREADS_PER_BLOCK=4` 得到 8。
   - 在 [src/gpu.sv:69](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L69) 找到 `NUM_FETCHERS = NUM_CORES` = 2。
   - 对照 [src/gpu.sv:85-134](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L85-L134) 两个实例的 `NUM_CONSUMERS / NUM_CHANNELS`，分别算过订阅率。
3. **需要观察的现象**：两个控制器都是 2 倍过订阅。
4. **预期结果**：data = 8/4 = 2；program = 2/1 = 2。
5. 待本地验证（纯阅读即可确认，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `NUM_CORES` 从 2 改成 4，data 控制器的过订阅率变成多少？
**答**：`NUM_LSUS = 4 × 4 = 16`，通道仍是 4，过订阅率 = 16/4 = 4。消费者翻倍，但通道数不变，拥塞加重。

**练习 2**：为什么过订阅率提高会让内核变慢？
**答**：通道数不变意味着「单位周期能服务的请求数」不变；消费者变多后，排在前面的请求占满通道，后面的请求只能在 `IDLE` 里空等更久才被拾取，单条访存的平均等待周期变长，整体执行周期数上升。

---

### 4.2 五态状态机：每通道一条独立的流水线

#### 4.2.1 概念说明

控制器不是「一个状态机管所有事」，而是**每条通道各自跑一份相同的状态机**。可以把每条通道想象成一个独立的「办事员」，他有自己的状态，独立地从消费者里叫号、去外部内存办事、把结果送回消费者。

每条通道的状态机有 5 个状态，分成「读」与「写」两条对称的支线：

- `IDLE`：空闲。扫描消费者，看谁有请求。
- `READ_WAITING` / `WRITE_WAITING`：已经把请求送到外部内存，正在等外部应答（`mem_*_ready`）。
- `READ_RELAYING` / `WRITE_RELAYING`：拿到外部应答了，正在把结果中继回消费者，**等消费者确认收下**。
- 回到 `IDLE`。

#### 4.2.2 核心流程

一条通道服务「一次读」的状态流转：

```
IDLE ──(扫到某消费者 read_valid)──> READ_WAITING
                                          │
                          (外部 mem_read_ready 拉高)
                                          ▼
                                    READ_RELAYING
                                          │
                       (消费者撤回 read_valid，表示已收下)
                                          ▼
                                        IDLE
```

写的支线对称，只是把 `read` 换成 `write`、`READ_RELAYING` 换成 `WRITE_RELAYING`。注意 `WAITING` 等的是**外部内存**，`RELAYING` 等的是**消费者**——两段等待的对象不同，这是初学时最容易混的地方。

#### 4.2.3 源码精读

5 个状态用 `localparam` 编码为 3 位（注意 `001` 没有被使用，这是个保留编码位）：

[src/controller.sv:38-42](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L38-L42) —— 五态定义：`IDLE=000`、`READ_WAITING=010`、`WRITE_WAITING=011`、`READ_RELAYING=100`、`WRITE_RELAYING=101`。

「每条通道一份状态」靠未打包数组实现——数组下标就是通道号 `i`：

[src/controller.sv:45-47](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L45-L47) —— `controller_state [NUM_CHANNELS-1:0]` 是每通道一份的状态；`current_consumer [NUM_CHANNELS-1:0]` 记录「本通道当前正在服务第几号消费者」；`channel_serving_consumer` 是「每位对应一个消费者」的共享去重位（见 4.4）。

主循环用 `for i` 遍历每条通道，**并发地**各自走状态机——这意味着 4 条 data 通道可以同一周期各自服务一个不同的消费者：

[src/controller.sv:68-69](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L68-L69) —— `for (int i = 0; i < NUM_CHANNELS; i++)` 外层循环：每条通道独立处理。注释「handle processing concurrently」点明了通道间的并发关系。

`case (controller_state[i])` 按当前通道的状态分派（[src/controller.sv:69](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L69)）。各状态分支的源码在后续模块逐一展开。

#### 4.2.4 代码实践

1. **实践目标**：确认「每条通道独立」这一结构，而不是「一个状态机」。
2. **操作步骤**：
   - 在 [src/controller.sv:45-46](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L45-L46) 数一数 `controller_state` 和 `current_consumer` 这两个数组的长度，是按 `NUM_CHANNELS` 还是 `NUM_CONSUMERS` 开的。
   - 在 [src/controller.sv:68](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L68) 确认外层循环变量 `i` 的范围。
3. **需要观察的现象**：状态与「当前消费者」都是按**通道**展开的；循环也是按**通道**遍历的。
4. **预期结果**：data 控制器里有 4 份 `controller_state`、4 份 `current_consumer`，4 条通道同周期可并行服务 4 个消费者。
5. 待本地验证（纯阅读）。

#### 4.2.5 小练习与答案

**练习 1**：`WAITING` 阶段等的是谁？`RELAYING` 阶段等的是谁？
**答**：`WAITING` 等外部内存回 `mem_*_ready`（[src/controller.sv:99](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L99) 读侧、[src/controller.sv:108](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L108) 写侧）；`RELAYING` 等消费者撤回 `consumer_*_valid` 表示已收下（[src/controller.sv:116](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L116) 读侧、[src/controller.sv:123](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L123) 写侧）。

**练习 2**：5 个状态为什么用 3 位编码却只用了 5 个值？
**答**：3 位可表示 0–7 共 8 个值，这里只用了 5 个（`000/010/011/100/101`），`001/110/111` 未用（其中 `001` 是保留位）。留空是为了读写两支线的编码在 bit[1] 上区分（读 `0`、写 `1`），bit[2] 区分 WAITING(`0`) 还是 RELAYING(`1`)，编码自带「读/写」与「等待/中继」的语义结构。

---

### 4.3 通道轮询消费者与 break

#### 4.3.1 概念说明

回到「叫号机」的比喻：当一条通道空闲（`IDLE`）时，它要决定下一个服务谁。tiny-gpu 用的策略非常朴素——**按消费者编号从 0 到 N-1 顺序扫描**，遇到第一个「有请求且还没被别的通道领走」的消费者就服务它，然后**立刻停手**（`break`），这一周期不再领第二个。

这是一种**固定优先级轮询**：编号小的消费者优先级更高。它简单、可预测，缺点是高编号消费者在繁忙时可能饿死（starvation）——这是 tiny-gpu 的简化之一，u7 会讨论。

#### 4.3.2 核心流程

`IDLE` 状态里，通道 `i` 的扫描逻辑（伪代码）：

```
for j = 0 .. NUM_CONSUMERS-1:
    if 读请求有效[j] 且 未被领走[j]:
        领走[j] = 1            # 阻塞赋值，立刻生效（见 4.4）
        当前服务对象[i] = j
        把请求转投到外部通道 i
        状态[i] = READ_WAITING
        break                  # 本周期只领一个，退出扫描
    else if 写请求有效[j] 且 未被领走[j]:
        （对称处理，转 WRITE_WAITING）
        break
```

两个要点：

1. **读优先于写**：对同一个消费者 `j`，先判 `consumer_read_valid` 再判 `consumer_write_valid`（虽然实际里一个 LSU 同一时刻只会发读或写之一）。
2. **`break` 保证「一通道一周期一请求」**：找到第一个候选就退出内层 `for j`，通道这一拍不再扫后续消费者。

#### 4.3.3 源码精读

`IDLE` 分支里的内层扫描循环：

[src/controller.sv:70-96](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L70-L96) —— `IDLE` 分支：`for (int j = 0; j < NUM_CONSUMERS; j++)` 顺序扫描消费者，先读后写，命中即拾取并 `break`。

读拾取分支（拉起对外部内存的读请求、切到 `READ_WAITING`）：

[src/controller.sv:73-82](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L73-L82) —— 命中 `consumer_read_valid[j] && !channel_serving_consumer[j]`：置去重位、记 `current_consumer[i]<=j`、把 `mem_read_valid[i]` 拉高、把消费者给出的地址 `consumer_read_address[j]` 透传到 `mem_read_address[i]`、状态切 `READ_WAITING`，然后 `break`。

写拾取分支对称，多透传一个写数据 `consumer_write_data[j]`：

[src/controller.sv:83-94](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L83-L94) —— 命中写请求：同样置去重位与 `current_consumer`，拉高 `mem_write_valid[i]`、透传地址与**写数据** `consumer_write_data[j]`，状态切 `WRITE_WAITING`，再 `break`。

注意地址透传这一步的「中继」本质：控制器自己不产生地址，它只是把消费者 `j` 给的 `consumer_read_address[j]` 原样搬到本通道的 `mem_read_address[i]` 上（[src/controller.sv:78](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L78)）。回程的数据中继见 4.5。

#### 4.3.4 代码实践

1. **实践目标**：理解「固定优先级 + 一通道一周期一请求」的扫描规则。
2. **操作步骤**：
   - 假设消费者 0、2、4 同一周期都有读请求，且都未被领走。在 [src/controller.sv:72-95](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L72-L95) 里手动模拟通道 0 的 `for j`：j=0 命中 → 拾取消费者 0 → `break`。
   - 问自己：通道 0 这一周期会去服务消费者 2 吗？
3. **需要观察的现象**：通道 0 只服务消费者 0，消费者 2、4 留给通道 1、2（如果它们也空闲）。
4. **预期结果**：因为有 `break`（[src/controller.sv:82](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L82)），一条通道一周期只拾取**一个**请求；编号小的优先。
5. 待本地验证（纯阅读推理）。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 `IDLE` 分支里的 `break`，会发生什么？
**答**：一条空闲通道会在同一周期里把它的 `current_consumer[i]`、`mem_read_valid[i]`、`mem_read_address[i]` 反复覆盖成扫描到的最后一个有效消费者，最终只服务编号最大的那个，且中途被覆盖的请求丢失——行为错乱。`break` 保证了「拾取即停」。

**练习 2**：这种「编号小优先」的扫描对高编号消费者有什么风险？
**答**：当低编号消费者持续有请求时，高编号消费者可能长期排不上队（饿死）。tiny-gpu 没有处理这个问题；真实 GPU 用更公平的轮转或年龄优先仲裁。这正是 u7-l1 要讨论的简化点之一。

---

### 4.4 channel_serving_consumer 去重：阻塞 vs 非阻塞的精妙

#### 4.4.1 概念说明

这是本讲**最关键、也最巧妙**的一处设计。问题来了：4 条通道同周期并发扫描，它们看到的消费者请求是**同一份**。如果通道 0 和通道 1 都在 `IDLE`，且消费者 3 有请求，会不会两条通道**都**把消费者 3 领走？

如果没有防护，会。因为通道 0 处理时用的是非阻塞赋值，它的「我领了消费者 3」这一信息要到**下一周期**才被通道 1 看到——同一周期内通道 1 仍然看到消费者 3「未被领走」，于是重复拾取，造成「一个请求被两条通道同时服务」的灾难。

解法是一个**共享的去重位寄存器** `channel_serving_consumer`（每位对应一个消费者），并在置位时用**阻塞赋值** `=` 而非 `<=`：

- 通道 0 领走消费者 3 时，**立刻**（阻塞）把 `channel_serving_consumer[3]` 置 1。
- 同一周期内，当外层 `for i` 循环推进到通道 1 时，通道 1 的扫描看到 `channel_serving_consumer[3]==1`，于是**跳过**消费者 3，改去领消费者 4（如果有的话）。

一句话：**阻塞赋值让「领走」在同一周期内对其它通道立即可见**，从而把并发拾取变成「各领各的、互不重复」。

#### 4.4.2 核心流程

去重位的生命周期：

```
通道 i 在 IDLE 命中消费者 j:
    channel_serving_consumer[j] = 1     # 阻塞 =，立刻生效 → 本周期其它通道可见
    （其余赋值用 <=，下一周期生效）
    ...
通道 i 在 RELAYING 完成服务、消费者已确认:
    channel_serving_consumer[j] = 0     # 阻塞 =，立刻释放 → 本周期即可被重新拾取
    状态[i] → IDLE
```

为什么只有这两处置位/复位用阻塞 `=`，而 `controller_state`、`mem_*`、`consumer_*_ready` 等都用非阻塞 `<=`？因为：

- 去重位是**跨通道共享**的协调信号，必须「即时可见」才能起到锁的作用——用阻塞。
- 状态机本身的数据通路（状态、对外请求、对消费者的应答）应遵循「寄存器一拍更新」的同步时序——用非阻塞，避免组合冒险。

这是一个非常教科书的「**协调用阻塞、数据用非阻塞**」的混合用法，值得细品。

#### 4.4.3 源码精读

去重位的声明与注释已经点明它的用途：

[src/controller.sv:47](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L47) —— `reg [NUM_CONSUMERS-1:0] channel_serving_consumer`，注释写道「Prevents many workers from picking up the same request」（防止多个 worker 重复拾取同一个请求）。

置位发生在 `IDLE` 的读/写拾取分支，注意是 `=`：

[src/controller.sv:74](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L74) —— 读拾取时 `channel_serving_consumer[j] = 1;`（阻塞）。

[src/controller.sv:84](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L84) —— 写拾取时同样 `channel_serving_consumer[j] = 1;`（阻塞）。

对照同一分支里**紧挨着**的其它赋值，全是 `<=`：

[src/controller.sv:75-79](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L75-L79) —— `current_consumer[i] <= j`、`mem_read_valid[i] <= 1`、`mem_read_address[i] <= ...`、`controller_state[i] <= READ_WAITING` 全用非阻塞。把这一段和上一行 `= 1` 放一起对比，阻塞/非阻塞的差异一目了然。

复位时也用阻塞把整个去重位清零（保证复位周期内就生效）：

[src/controller.sv:65](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L65) —— `channel_serving_consumer = 0;`（阻塞，整位清零），而它周围 `consumer_*_ready <= 0`、`controller_state <= 0` 等都用非阻塞。

释放发生在 `RELAYING` 末端，同样用阻塞 `=`：

[src/controller.sv:117](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L117) —— 读中继完成时 `channel_serving_consumer[current_consumer[i]] = 0;`（阻塞释放）。

[src/controller.sv:124](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L124) —— 写中继完成时同样阻塞释放。

最后，`IDLE` 扫描的命中条件本身就包含了对这个去重位的检查，这正是「看到被锁就跳过」的实现：

[src/controller.sv:73](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L73) —— `if (consumer_read_valid[j] && !channel_serving_consumer[j])`：只有「有请求」**且**「未被别的通道领走」才会拾取。第二个条件就是去重锁的读端。

#### 4.4.4 代码实践

1. **实践目标**：亲手验证「阻塞赋值让去重在同周期内生效」这一论断。
2. **操作步骤**：
   - 在 [src/controller.sv:72-95](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L72-L95) 假设一个场景：周期开始时 `channel_serving_consumer` 全 0，消费者 0 和消费者 1 都有读请求，4 条通道都 `IDLE`。
   - 手动展开外层 `for i`：
     - i=0：j=0 命中 → `channel_serving_consumer[0]=1`（阻塞，立刻为 1）→ `break`。
     - i=1：j=0 命中条件 `!channel_serving_consumer[0]`？此时已是 `1`，**跳过**；j=1 命中 → `channel_serving_consumer[1]=1` → `break`。
     - i=2、3：消费者 0、1 都被锁，扫描无命中，保持 `IDLE`。
3. **需要观察的现象**：通道 0 领走消费者 0，通道 1 领走消费者 1，**没有重复**。若去重位改用非阻塞 `<=`，则 i=1 时仍会看到 `channel_serving_consumer[0]==0`，于是通道 1 也领走消费者 0 → 重复。
4. **预期结果**：阻塞赋值下，4 条通道同周期最多领走 4 个**不同**的消费者。
5. 待本地验证（纯阅读推理；若想实测，见综合实践里「给控制器加观测」的可选增强）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `channel_serving_consumer` 的置位不能用非阻塞 `<=`？
**答**：非阻塞赋值要到本周期结束才更新、下一周期才被读到。若用它，同一周期内通道 1 扫描时仍看到 `channel_serving_consumer[0]==0`，会和通道 0 重复拾取消费者 0。必须用阻塞 `=` 让「领走」信息在本周期内即时可见。

**练习 2**：`channel_serving_consumer` 什么时候被清回 0？为什么这个清零也用阻塞？
**答**：在 `RELAYING` 末端、消费者确认收下后清零（[src/controller.sv:117](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L117)、[src/controller.sv:124](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L124)）。用阻塞是为了让该消费者在本周期内就能被另一条刚进入 `IDLE` 扫描的通道重新拾取（如果它恰好又发了新请求），保持锁的即时可用性。

---

### 4.5 READ/WRITE 中继应答时序

#### 4.5.1 概念说明

拾取之后，请求被透传到外部内存（见 4.3）。接下来的事就是「中继」：等外部内存把数据/确认送回来，再原样转交给当初发起请求的那个消费者。控制器的角色在这一阶段从「请求方」（对外部内存）切换回「应答方」（对消费者）。

关键在于**回程要找对人**：通道 `i` 这一拍在服务消费者 `current_consumer[i]`，所以外部回来的数据要精确地扔回 `consumer_read_data[current_consumer[i]]`，而不是别的消费者。`current_consumer[i]` 这个寄存器就是为此而存在的「回执地址」。

#### 4.5.2 核心流程

读的回程（`READ_WAITING → READ_RELAYING → IDLE`）：

```
READ_WAITING:
    if mem_read_ready[i] 拉高:            # 外部内存把数据备好了
        关掉对外请求 mem_read_valid[i] <= 0
        consumer_read_ready[current_consumer[i]] <= 1   # 告诉消费者「数据来了」
        consumer_read_data [current_consumer[i]] <= mem_read_data[i]  # 扔回数据
        状态[i] → READ_RELAYING

READ_RELAYING:
    if !consumer_read_valid[current_consumer[i]]:        # 消费者撤回 valid = 已收下
        释放去重锁 channel_serving_consumer[...] = 0
        consumer_read_ready[current_consumer[i]] <= 0
        状态[i] → IDLE
```

写的回程对称，但**写不回传数据**——消费者只需要一个「写完成」的确认（`consumer_write_ready`），所以 `WRITE_WAITING → WRITE_RELAYING` 不搬数据。

#### 4.5.3 源码精读

读等待与回程数据中继：

[src/controller.sv:97-105](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L97-L105) —— `READ_WAITING`：看到外部 `mem_read_ready[i]` 后，关掉 `mem_read_valid[i]`，把 `mem_read_data[i]` 扔回**当前服务的消费者**（用 `current_consumer[i]` 作下标）的 `consumer_read_data`，并拉高其 `consumer_read_ready`，状态切 `READ_RELAYING`。注意数据搬运用 `<=`，下一周期对消费者生效。

读中继完成与回收：

[src/controller.sv:115-121](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L115-L121) —— `READ_RELAYING`：等消费者撤回 `consumer_read_valid`（即 LSU 收下数据后撤掉请求，见 4.5.4 的 LSU 对手方），然后阻塞释放去重锁、非阻塞拉低 `consumer_read_ready`、状态回 `IDLE`。注释「Wait until consumer acknowledges it received response, then reset」说的正是这段。

写等待与回程（无数据回传）：

[src/controller.sv:106-113](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L106-L113) —— `WRITE_WAITING`：看到外部 `mem_write_ready[i]` 后，关掉 `mem_write_valid[i]`，只拉高 `consumer_write_ready`（确认写完成），状态切 `WRITE_RELAYING`。**没有**搬运数据——数据在拾取时已经随地址一起透传出去了（[src/controller.sv:89](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L89)）。

[src/controller.sv:122-128](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L122-L128) —— `WRITE_RELAYING`：等消费者撤回 `consumer_write_valid`，释放去重锁、拉低 `consumer_write_ready`、状态回 `IDLE`。

为了看清「回程找对人」，再看一眼消费者一侧 LSU 是怎么配合的——它在 `WAITING` 状态看到 `mem_read_ready`（也就是控制器扔回来的 `consumer_read_ready`）后收下数据并撤掉自己的 `read_valid`，正是控制器 `READ_RELAYING` 等待的那个「撤回」信号：

[src/lsu.sv:59-70](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L59-L70) —— LSU 读路径：`REQUESTING` 拉高 `mem_read_valid`（[src/lsu.sv:60-61](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L60-L61)），`WAITING` 看到 `mem_read_ready==1` 后把数据存进 `lsu_out` 并 `mem_read_valid <= 0`（[src/lsu.sv:64-69](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L64-L69)）。这「撤回 valid」一步，正是控制器 `READ_RELAYING` 退出的触发条件。两侧信号是同一根线：LSU 的 `mem_read_valid` = 控制器的 `consumer_read_valid`。

#### 4.5.4 代码实践（本讲主任务）

> 阅读本任务对应规格：**追踪一次完整的 data 内存读：从某 LSU 的 read_valid 拉高，到控制器中继，再到 consumer_read_ready 回送，标注每个状态停留的周期数。**

1. **实践目标**：把「LSU 发请求 → 控制器拾取 → 控制器等外部 → 控制器扔回数据 → LSU 确认 → 控制器回收」这条闭环在脑子里跑一遍，并标注每个状态大致停留的周期数。
2. **先看清一个事实**：现有日志**看不到**控制器状态。[test/helpers/format.py:78-86](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L78-L86) 定义了 `format_memory_controller_state`，但 [test/helpers/format.py:97-141](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L97-L141) 的 `format_cycle` **从未调用它**——日志只打印 PC、指令、Core/Fetcher/LSU 状态、寄存器。所以这个实践主要是**读 RTL 推理**（如下），精确周期数待本地验证。
3. **操作步骤（读 RTL 推理）**：选定消费者 `c`（某个 LSU），假设它进入读流程时控制器通道 `i` 正好空闲。按非阻塞赋值「下一周期生效」的规则逐步推理（`A` 表示 LSU 首次满足 `core_state==REQUEST` 的周期）：
   - 周期 `A`：LSU `IDLE→REQUESTING`（[src/lsu.sv:53-57](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L53-L57)）。控制器仍 `IDLE`，看到的 `consumer_read_valid[c]` 还是 0。
   - 周期 `A+1`：LSU `REQUESTING→WAITING`，`mem_read_valid<=1`（[src/lsu.sv:59-63](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L59-L63)）。
   - 周期 `A+2`：`consumer_read_valid[c]=1` 首次对控制器可见。通道 `i` 在 `IDLE` 扫描命中 → 置去重位、记 `current_consumer[i]=c`、拉高 `mem_read_valid[i]`、状态切 `READ_WAITING`（[src/controller.sv:73-79](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L73-L79)）。**此周期通道处于 `IDLE`。**
   - 周期 `A+3`：通道 `i` 进入 `READ_WAITING`。`mem_read_valid[i]=1` 已对外可见，外部内存（[test/helpers/memory.py:37-42](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/memory.py#L37-L42)）同拍回 `mem_read_ready[i]=1` 与数据。控制器据此把数据扔回 `consumer_read_data[c]`、拉高 `consumer_read_ready[c]`、切 `READ_RELAYING`（[src/controller.sv:99-104](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L99-L104)）。**`READ_WAITING` 停留约 1 周期**（得益于外部内存零延迟模型）。
   - 周期 `A+4`：通道 `i` 进入 `READ_RELAYING`，`consumer_read_ready[c]=1` 对 LSU 可见。LSU 在 `WAITING` 看到 ready → 收下数据、撤回 `mem_read_valid`（[src/lsu.sv:64-69](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/lsu.sv#L64-L69)）。但控制器此拍仍看到 `consumer_read_valid[c]==1`（LSU 的撤回要下拍才生效），故**停留**在 `READ_RELAYING`。
   - 周期 `A+5`：`consumer_read_valid[c]==0` 对控制器可见，触发 `READ_RELAYING→IDLE`，释放去重锁、拉低 `consumer_read_ready[c]`（[src/controller.sv:116-120](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L116-L120)）。**`READ_RELAYING` 停留约 2 周期**（要等消费者撤回 valid 这一拍）。
4. **需要观察的现象**：`READ_WAITING` 比 `READ_RELAYING` 短——前者只等外部（零延迟模型下约 1 拍），后者还要等消费者「撤回 valid」这一拍确认，故约 2 拍。
5. **预期结果（推理值，待本地验证）**：`IDLE`(拾取当拍) → `READ_WAITING` ≈1 拍 → `READ_RELAYING` ≈2 拍 → 回 `IDLE`。写路径对称，但 `WRITE_WAITING/WRITE_RELAYING` 不搬数据。
6. **可选增强（你本地的实验，不属于项目原有代码）**：若想真正看到周期数，可在 `controller.sv` 的 `for i` 循环末尾临时加一句 `$display("ch=%0d state=%0d cons=%0d", i, controller_state[i], current_consumer[i]);`，重跑 `make test_matadd`，在标准输出里数每条通道在各状态停留的拍数；或把 `format_cycle` 里补一行调用 `format_memory_controller_state` 打印某条通道状态。这些改动仅用于本地观察，验证后请还原。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `READ_RELAYING` 不能像 `READ_WAITING` 那样 1 拍就结束？
**答**：`READ_WAITING` 等的是外部内存，而外部内存在仿真里是零延迟同拍应答，所以约 1 拍。`READ_RELAYING` 等的是消费者撤回 `consumer_read_valid`——控制器把 `consumer_read_ready` 拉高后，LSU 要到**下一拍**才能据此撤回 valid，控制器又要到**再下一拍**才看到这个撤回。这「一来一回」至少多花 1 拍，所以约 2 拍。

**练习 2**：读路径要回传数据，写路径不回传数据，为什么？
**答**：读的目的是把内存里的值送给消费者，所以控制器要把 `mem_read_data` 扔回 `consumer_read_data`（[src/controller.sv:102](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L102)）。写的目的是把消费者的值写进内存，数据在拾取阶段（`IDLE`）就已经随地址一起透传到外部了（[src/controller.sv:89](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L89)），回程只需要告诉消费者「写完成」（`consumer_write_ready`），无需回传数据。

---

## 5. 综合实践

把本讲五个最小模块串起来，完成下面这个**「从一次 data 内存读，反推控制器的全部机制」**的任务：

1. **定位规模**（4.1）：在 [src/gpu.sv:85-91](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/gpu.sv#L85-L91) 确认 data 控制器 `NUM_CONSUMERS=8、NUM_CHANNELS=4`，写下过订阅率 = 2。
2. **画出状态机**（4.2）：在 [src/controller.sv:38-42](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L38-L42) 抄下五态，画出 `IDLE→READ_WAITING→READ_RELAYING→IDLE` 的图，每条边上标注触发条件（哪个信号拉高/撤回）。
3. **解释扫描**（4.3）：用自己的话说明 [src/controller.sv:72-95](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L72-L95) 里 `for j` + `break` 如何保证「一条通道一周期只领一个、小编号优先」。
4. **论证去重**（4.4）：回答——如果 [src/controller.sv:74](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L74) 的 `channel_serving_consumer[j] = 1` 改成 `<= 1`，4 条并发通道会发生什么？给出「同一请求被多通道重复拾取」的推理过程。
5. **标注时序**（4.5）：按 4.5.4 的推理，写出一次读中 `controller_state[i]` 在连续周期里的取值序列（如 `IDLE, IDLE, READ_WAITING, READ_RELAYING, READ_RELAYING, IDLE`），并标注每个状态停留拍数（待本地验证）。
6. **产出**：一张包含「状态机图 + 扫描规则 + 去重锁说明 + 时序序列」的单页笔记。

完成后，你就把 `controller.sv` 这一个文件从「规模参数 → 状态机 → 仲裁规则 → 去重机制 → 中继时序」完整吃透了。

## 6. 本讲小结

- `controller` 存在的根本原因是**带宽瓶颈**：消费者（data 控制器 8 个 LSU、program 控制器 2 个 fetcher）多于外部通道（分别 4 条、1 条），需要仲裁中继（[src/controller.sv:4-7](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L4-L7)）。
- 每条通道独立跑一份**五态状态机** `IDLE / READ_WAITING / WRITE_WAITING / READ_RELAYING / WRITE_RELAYING`，状态按通道展开（[src/controller.sv:38-47](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L38-L47)），外层 `for i` 让多通道并发（[src/controller.sv:68](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L68)）。
- `IDLE` 用 `for j` **固定优先级轮询**消费者，靠 `break` 保证「一通道一周期一请求」（[src/controller.sv:70-96](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L70-L96)）。
- `channel_serving_consumer` 是跨通道共享的**去重锁**：置位/复位用**阻塞** `=` 让「领走/释放」同周期内对其它通道可见，而数据通路用**非阻塞** `<=` 保持同步时序（[src/controller.sv:65](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L65)、[src/controller.sv:74](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L74)、[src/controller.sv:84](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L84)、[src/controller.sv:117](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L117)、[src/controller.sv:124](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L124)）。
- 中继靠 `current_consumer[i]` 这个「回执地址」把外部应答精确扔回当初的消费者；读回传数据、写只回确认（[src/controller.sv:97-128](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/src/controller.sv#L97-L128)）。
- 现有日志**看不到**控制器状态——`format_memory_controller_state` 已定义但 `format_cycle` 从未调用它（[test/helpers/format.py:78-86](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/test/helpers/format.py#L78-L86)），因此周期级观测需自行加 `$display` 或扩展 `format_cycle`（本地实验）。

## 7. 下一步学习建议

本讲把 `controller.sv` 内部机制讲完了。下一步建议：

- **u4-l1 Core 解剖结构** / **u4-l2 Scheduler 核心状态机**——看 core 如何在 `REQUEST` 阶段统一驱动所有 LSU 发起请求、在 `WAIT` 阶段等所有 LSU 收齐，从而理解控制器面对的「8 个消费者往往同时发请求」的拥塞从何而来。
- **u5-l3 LSU 异步访存**——下钻 LSU 的 `IDLE/REQUESTING/WAITING/DONE` 四态，把它和本讲的控制器五态配成一对完整的「请求方↔中继方」握手时序图。
- **u7-l2 内存优化与缓存**——本讲的控制器是「单层、无合并、固定优先级」的朴素仲裁；真实 GPU 用多层缓存、内存合并（coalescing）、共享内存来缓解这里暴露的带宽瓶颈，到时候可以回头对照 `controller.sv` 找改进点。
