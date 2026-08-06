# 资源共享：共享 ChaCha20/Poly1305 流水线

## 1. 本讲目标

本讲是 Unit 5（ChaCha20-Poly1305 加密硬件）的「资源复用」篇，承接 u5-l3（加密数据流）与 u5-l4（解密与验证数据流）。前面两讲我们分别读了「独立加密」和「独立解密」两条 datapath——每条都各自例化了一套 ChaCha20 流水线和一套 Poly1305 计算核。本讲要回答一个面积（area）与吞吐（throughput）的取舍问题：

> 加密和解密用的是**同一套** ChaCha20/Poly1305 算法逻辑，能不能不复制两份，而是**共用一份**硬件，让它一会儿服务加密、一会儿服务解密？

学完本讲，你应当能够：

- 说清**为什么要共享**：ChaCha20 的 64 级流水线是整个 SoC 里最「重」的组合逻辑，复制两份代价高昂；共享一份能省下接近一半的面积。
- 读懂**ID 标签穿越流水线**这个核心机制：用一个 1 比特的 `is_encrypt` 标签在请求**注入时**盖戳，让它随数据穿过全部 64 级，在**出口处**再按这个标签把结果路由回正确的请求方。
- 读懂**输入端 round-robin 复用**：一个每拍翻转的 `static` 寄存器，轮流给加密和 decrypt 各一拍的「时间片」，公平但固定 50/50。
- 读懂**输出端 ID 解复用与背压（backpressure）**：出口按「穿过来的标签」分发，并正确地把 ready 信号回传；同时看清当前代码里 **ChaCha20 共享是「正确处理背压」的**，而 **Poly1305 共享是一份「未完工」的存档**。
- 记住一个**必须如实标注的现状**：当前 HEAD 的顶层实际**只共享了 ChaCha20**，Poly1305 的共享文件虽然存在于源码、被 README 描述，但被注释掉了，改用每侧独立的 MCP 实例来换吞吐。

## 2. 前置知识

本讲默认你已经掌握以下内容（若不熟请先回看对应讲义）：

- **PipelineC 工作流与三种设计变体**（u5-l2）：PipelineC 把每个 C 函数编译成一条流水线；本项目有三种顶层变体——独立加密、独立解密、加解密共享（shared）。变体之间靠 `#define` 实例名前缀 + `#include` 同一份 `.c` 来复用代码。关键宏约定：`GLOBAL_VALID_READY_PIPELINE_INST(实例名, 输出类型, 计算函数, 输入类型, 级数)` 会生成一条带 `valid`/`ready` 握手的、指定级数的全局流水线实例，并自动产生 `<实例名>_in` / `<实例名>_in_ready` / `<实例名>_out` / `<实例名>_out_ready` 四根全局线。
- **加密数据流**（u5-l3）：明文→ChaCha20→密文分叉→……。重点回忆：ChaCha20 流水线吃 512 位（64 字节）一个块、进出各有一次 128↔512 位宽转换；它既算密文，又用 counter=0 的首块派生 Poly1305 一次性密钥 `poly_key`。
- **AXIS 握手**（u5-l2/u5-l3）：`valid` 与 `ready` 同拍都为 1，才完成一次 beat 传输；**背压**就是下游用 `ready=0` 暂时拒绝接收，上游必须停住不能丢数据。

一个关键直觉，先建立起来：

- 加密和解密的 ChaCha20 **计算逻辑完全相同**（流密码，解密就是把密文再喂进同一套逻辑）。所以共享的不是「两份不同的功能」，而是「同一份功能被两个主人轮流调用」。这与软件里「一个线程池服务多种任务」是同一类思路——硬件版叫**时分复用（time-division multiplexing）**。
- 既然是「轮流」，就必须解决两个问题：**入口处**谁先谁后（调度），**出口处**结果还给谁（路由）。本讲的全部源码都是围绕这两件事。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [chacha20poly1305_encrypt_decrypt_shared.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305_encrypt_decrypt_shared.c) | **共享变体的顶层**。决定「共享谁、不共享谁」。当前 HEAD 在此把 ChaCha20 共享 `#include` 进来，把 Poly1305 共享**注释掉**。这是看懂「实际编译了什么」的入口。 |
| [chacha20_pipeline_shared.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20_pipeline_shared.c) | **ChaCha20 共享的主体**（当前实际生效）。定义带 `is_encrypt` 标签的包裹结构体、唯一一条 64 级共享流水线、四对「虚拟」加密/解密接口线，以及调度+路由的 `chacha20_sharing_mux`。本讲的最核心文件。 |
| [poly1305_pipeline_shared.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_pipeline_shared.c) | **Poly1305 共享的「存档」**（当前**未编译**）。用另一种宏 `GLOBAL_PIPELINE_INST_W_VALID_ID` 自动携带 ID，背压处理尚未完工。本讲用它做对比，讲清两种 ID 携带手法与「未完工」的差异。 |
| [encrypt_shared.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/encrypt_shared.c) | 共享设计里的「加密半边」。用 `CHACHA_EXCLUDES_PIPELINE` 关掉本侧自带的 ChaCha20 流水线，只保留做位宽转换的 FSM，从而挂接到共享流水线上。 |
| [chacha20.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20.c) | ChaCha20 模块本体。`CHACHA_EXCLUDES_PIPELINE` 这道开关就在这里——它控制「要不要在本侧例化流水线」。理解共享如何「抠掉」本侧流水线，必须看这里。 |
| [poly1305_mac.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_mac.c) | Poly1305 MAC 模块本体。当前实际生效的是每侧一个 **MCP（Multi-Cycle Path）**实例，而非共享流水线；这里能看到「共享流水线方案被注释、改用 MCP」的代码痕迹。 |

辅助理解（非本讲精读对象）：

- `decrypt_shared.c`：共享设计的「解密半边」，结构与 `encrypt_shared.c` 对称。
- `build_verilog_shared.sh` / `build_sim_*_shared.sh`：共享变体的综合/仿真脚本，本讲实践会用到。

---

## 4. 核心概念与源码讲解

本讲按「先建立共享的心智模型，再分别拆输入调度、输出路由与背压」的顺序，分三个最小模块：

- **4.1 共享流水线的动机与 ID 标签传播**：为什么共享、怎么把「这是谁的请求」这个 1 比特身份盖到数据上、又怎么让它原样穿过 64 级流水线。这是其余两块的地基。
- **4.2 输入端 round-robin 复用**：入口处怎么轮流挑加密/解密喂进唯一一条流水线。
- **4.3 输出端 ID 解复用与背压**：出口处怎么按「穿过来的标签」分发结果，并正确回传 ready；同时如实看清 ChaCha20 与 Poly1305 两份共享代码在背压处理上的真实差距，以及「Poly1305 共享当前未编译」这一现状。

---

### 4.1 共享流水线的动机与 ID 标签传播

#### 4.1.1 概念说明

先说**动机**。在独立加密（u5-l3）和独立解密（u5-l4）两种变体里，加密侧有自己的 ChaCha20 流水线，解密侧也有自己的一条——**两条一模一样的 64 级流水线**。ChaCha20 的核心是 `chacha20_block`，里面是 10 次 `chacha20_block_step`、每步 8 个并行 quarter round，全展开成巨大的组合逻辑，再流水线切成 64 级才跑得上 80 MHz。这是整个加密核里**最贵**的部分。复制两份，意味着双倍的逻辑单元（LUT/FF）、双倍的面积。

但是，加密和解密的 ChaCha20 计算逻辑**完全相同**——它只是用 key/nonce/counter 生成密钥流再异或。于是有一个很自然的省钱办法：

> 只造**一条** ChaCha20 流水线，让它一会儿算加密的块、一会儿算解密的块。

这就是**资源共享（resource sharing）/ 时分复用**。代价是吞吐：一条流水线被两个主人瓜分，每个主人**最多**只能用到一半的拍子。所以这是典型的「**以吞吐换面积**」取舍——共享变体面积接近独立变体的一半，但每侧峰值吞吐也减半。

再说**难点**。一条流水线有 64 级，意味着一个请求从入口进去，要过 64 拍才从出口出来。在这 64 拍里，入口处的调度器早就不知道转去服务谁了（它在每拍轮流切换）。那么，当一个结果从出口冒出来时，**怎么知道它属于加密还是解密？**

答案是**身份标签（ID tag）**：在请求**注入流水线的那一刻**，给它的「信封」上盖一个 1 比特的戳——`is_encrypt`（1=这是加密的请求，0=这是解密的请求）。这个戳**作为数据的一部分，随数据一起穿过全部 64 级**。64 拍后结果出来时，出口只要读这封信封上的戳，就知道该把结果路由回哪一侧。

这就像邮局分拣：你寄信时在信封上写好「寄给加密科」或「寄给解密科」，信在分拣中心（64 级流水线）里转悠很久，到了出口，工作人员**只看信封上的地址**来投递，根本不需要记得这封信是几点几分进来的。

关键术语：

- **时分复用（time-division multiplexing）**：一份硬件轮流服务多个请求方。
- **ID 标签传播（ID tag propagation）**：身份比特随数据穿过流水线，出口据此路由。
- **信封（envelope）**：把原始数据连同 ID 一起打包的结构体。

#### 4.1.2 核心流程

把 ChaCha20 的「共享 + 标签传播」画成数据流，大致是这样：

```
                ┌──────────────── 加密请求 (chacha20_loop_body_in_t) ────────┐
                │                                                              │
  入口 round-   │   ┌────────────────────────────────────────────────────┐    │
  robin 调度 ───┼──▶│ 盖戳: 把 data 包进 (data, is_encrypt) 信封          │    │
  (4.2 讲)      │   └────────────────────┬───────────────────────────────┘    │
                │                        ▼                                      │
                │           ┌─────────────────────────────────┐               │
                └──────────▶│  唯一一条 64 级 chacha20_pipeline │◀──────────┐  │  解密请求
                            │  (信封里 is_encrypt 一路同行 64 拍)│           │  │
                            └─────────────────┬───────────────────┘           │
                                              ▼                                │
                            ┌─────────────────────────────────┐               │
                            │ 拆信封: 读出来的 is_encrypt       │               │
                            └─────────────────┬───────────────────┘           │
                                              ▼                                │
  出口 ID 解复用 ── is_encrypt=1 ──▶ 加密结果   |   is_encrypt=0 ──▶ 解密结果 ──┘
  (4.3 讲)
```

三个要点先记住：

1. **只有一条物理流水线**（`chacha20_pipeline`，64 级），加密和解密**看起来**各自有 `chacha20_encrypt_pipeline_*` / `chacha20_decrypt_pipeline_*` 接口，但那只是「虚拟接口」，背后都指向这同一条。
2. **标签在注入时盖、在出口读**——注入用的是「当时的」`is_encrypt`，出口读的是「数据自己带出来的」`is_encrypt`，两者相隔 64 拍，互不干扰。
3. **原始计算函数一个字都不用改**。被共享的 `chacha20_loop_body` 只认原始的 `chacha20_loop_body_in_t`，对标签毫无感知；标签是「包裹」在外面的一层。

#### 4.1.3 源码精读

**第一步：先看顶层到底共享了谁。** 这是理解全篇的前提。共享变体的顶层是 `chacha20poly1305_encrypt_decrypt_shared.c`：

[chacha20poly1305_encrypt_decrypt_shared.c:9-18](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305_encrypt_decrypt_shared.c#L9-L18) —— 决定共享什么、不共享什么。

```c
// The shared parts between encrypt and decrypt
// the two big compute pipelines
#include "chacha20/chacha20_pipeline_shared.c"
// Actually Poly1305 isnt that big, better to not-share pipeline resources
// and get throughput improvement from MCP instead
//#include "poly1305/poly1305_pipeline_shared.c"

// The encrypt and decrypt specifics part of the shared design
#include "chacha20poly1305/encrypt_shared.c"
#include "chacha20poly1305/decrypt_shared.c"
```

读这段代码要抓住两个事实：

- **ChaCha20 共享是「生效」的**：第 11 行 `#include "chacha20/chacha20_pipeline_shared.c"` 没有被注释，会被编译进设计。
- **Poly1305 共享是「被注释掉」的**：第 14 行 `//#include "poly1305/poly1305_pipeline_shared.c"` 前面有 `//`。紧接着的注释给出了原因——「Poly1305 其实没那么大，与其共享流水线，不如不共享、用 MCP（多周期路径）来换更高的吞吐」。

这一点很重要，因为项目的 README 把 ChaCha20 和 Poly1305 都描述成「共享」，但**当前 HEAD 的代码实际只共享了 ChaCha20**。我们本讲会两个文件都讲（Poly1305 共享文件本身是重要的对比教材），但你要始终清楚：**真正编进 bitstream 的是「只共享 ChaCha20」这个组合**。

**第二步：看 ChaCha20 怎么把 ID 盖到信封上。** 进入 `chacha20_pipeline_shared.c`。它先定义了带标签的「信封」结构体：

[chacha20_pipeline_shared.c:11-20](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20_pipeline_shared.c#L11-L20) —— 把原始数据 + 1 比特 `is_encrypt` 打包成信封。

```c
typedef struct chacha_shared_pipeline_in_t{
  chacha20_loop_body_in_t data;
  uint1_t is_encrypt;
}chacha_shared_pipeline_in_t;
DECL_STREAM_TYPE(chacha_shared_pipeline_in_t)
typedef struct chacha_shared_pipeline_out_t{
  axis512_t data;
  uint1_t is_encrypt;
}chacha_shared_pipeline_out_t;
DECL_STREAM_TYPE(chacha_shared_pipeline_out_t)
```

注意输入信封 `chacha_shared_pipeline_in_t` 里有原始的 `data`（类型是 `chacha20_loop_body_in_t`，即 key/nonce/counter/一个 512 位块）外加 `is_encrypt`；输出信封里是算完的 `axis512_t`（密文/密钥流块）外加**同一个** `is_encrypt`。两边的 `is_encrypt` 字段就是用来「让标签过流水线」的载体。

**第三步：写一个「原样搬运标签」的包裹函数。** 原始计算函数 `chacha20_loop_body` 只认 `chacha20_loop_body_in_t`，不认信封。于是这里写了一个小包裹 `chacha_shared_pipeline`：拆信封→调原始函数→把结果和新信封装回去，**关键是把入端的 `is_encrypt` 原封不动抄到出端**：

[chacha20_pipeline_shared.c:22-29](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20_pipeline_shared.c#L22-L29) —— 包裹函数：算的是原来的 ChaCha20，但把 ID 一路抄到输出。

```c
chacha_shared_pipeline_out_t chacha_shared_pipeline(chacha_shared_pipeline_in_t inputs){
  chacha_shared_pipeline_out_t outputs;
  outputs.data = chacha20_loop_body(inputs.data);
  outputs.is_encrypt = inputs.is_encrypt;   // 标签原样随数据穿过
  return outputs;
}
// TODO use macro that includes ID instead like poly1305?
GLOBAL_VALID_READY_PIPELINE_INST(chacha20_pipeline, chacha_shared_pipeline_out_t, chacha_shared_pipeline, chacha_shared_pipeline_in_t, 64)
```

最后那行宏才是「造硬件」的地方：`GLOBAL_VALID_READY_PIPELINE_INST(实例名=chacha20_pipeline, 输出类型, 计算函数=chacha_shared_pipeline, 输入类型, 级数=64)`。它会生成**唯一一条** 64 级、带 `valid`/`ready` 握手的流水线实例，并自动产生全局线 `chacha20_pipeline_in` / `chacha20_pipeline_out` 等。因为流水线里跑的是「带标签的包裹函数」，所以标签自然就随数据走了 64 级。

第 28 行那句 `TODO use macro that includes ID instead like poly1305?` 是个重要线索：作者自己也知道这种「手写结构体抄标签」的做法有点笨，Poly1305 那边用的是宏自带 ID 的更省事写法（见 4.3 节对比）。这正好引出本讲的对比主题。

**第四步（补充）：共享如何「抠掉」每侧自带的流水线。** 你可能会问：加密侧 `chacha20_encrypt` 这个 FSM 本来在独立变体里会自己例化一条 ChaCha20 流水线，现在共享了，这条多余的流水线怎么去掉？答案在 `encrypt_shared.c`：

[encrypt_shared.c:13-15](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20poly1305/encrypt_shared.c#L13-L15) —— 用宏关掉本侧自带的流水线。

```c
#define CHACHA_INST chacha20_encrypt
#define CHACHA_EXCLUDES_PIPELINE
#include "../chacha20/chacha20.c"
```

`#define CHACHA_EXCLUDES_PIPELINE` 这个开关，在 `chacha20.c` 里被这样使用：

[chacha20.c:32-35](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20.c#L32-L35) —— 没定义这个开关才例化本侧流水线。

```c
#ifndef CHACHA_EXCLUDES_PIPELINE
// Global instance of the chacha20_loop_body pipeline
GLOBAL_VALID_READY_PIPELINE_INST(PPCAT(CHACHA_INST,_pipeline), axis512_t, chacha20_loop_body, chacha20_loop_body_in_t, 64)
#endif
```

也就是说：

- **独立变体**：不定义 `CHACHA_EXCLUDES_PIPELINE` → 加密侧自己例化一条 `chacha20_encrypt_pipeline`、解密侧自己例化一条 `chacha20_decrypt_pipeline`，两条独立流水线。
- **共享变体**：两侧都定义 `CHACHA_EXCLUDES_PIPELINE` → 本侧**不**例化流水线，只保留做 128↔512 位宽转换的 FSM（`chacha20_encrypt` / `chacha20_decrypt`）。这个 FSM 照常往 `chacha20_encrypt_pipeline_in` / `chacha20_decrypt_pipeline_in` 这些全局线上写——只是这些线现在**不再连到本侧流水线，而是连到共享的 `chacha20_sharing_mux`**。

于是对加解密 FSM 来说，它「以为自己」还连着一条专属流水线，接口名都没变；底下其实被偷偷换成了共享调度。这就是共享能做到「对上层透明」的秘诀。

#### 4.1.4 代码实践

**实践目标**：亲手确认「被共享的计算函数本身对标签毫无感知」这一点。

**操作步骤**：

1. 打开 [chacha20.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20.h)，找到 `chacha20_loop_body` 函数（定义在 `chacha20.h` 里，因为 PipelineC 用 `.h` 放可综合函数）。
2. 检查它的输入类型 `chacha20_loop_body_in_t` 和输出类型 `axis512_t`，确认：**输入里没有任何 `is_encrypt` 字段**。
3. 回到 `chacha20_pipeline_shared.c` 的 `chacha_shared_pipeline`，确认标签是**在外面那层包裹**加进去的，`chacha20_loop_body(inputs.data)` 这一句只拿到剥掉信封的原始数据。

**需要观察的现象**：`chacha20_loop_body` 的签名里搜不到任何「encrypt/decrypt/id」字样；它是一个纯粹的「输入一个块、输出一个块」的函数。

**预期结果**：你应当能得出结论——**共享不需要改动任何加密算法代码**，只需要在外面套一层「带标签的信封」+ 一个调度器。这也是为什么共享方案能在不大动独立变体的前提下「插」进来。

> 说明：以上是源码阅读型实践，无需运行工具链即可完成。

#### 4.1.5 小练习与答案

**练习 1**：如果把共享流水线的级数从 64 改成 32（假设时序仍能收敛），出口处的路由逻辑需要改吗？

**参考答案**：**不需要改路由逻辑**。出口路由只读 `chacha20_pipeline_out.data.is_encrypt` 这个随数据穿过来的标签，与流水线有几级无关。级数只影响「一个请求从进到出的延迟」（32 拍 vs 64 拍），不影响「按标签分发」的正确性——这正是「标签随数据走」设计的好处。

**练习 2**：为什么作者在 `chacha_shared_pipeline` 里要写 `outputs.is_encrypt = inputs.is_encrypt;`，能不能省掉这行？

**参考答案**：**不能省**。这行是把入端盖的戳「抄」到出端，正是标签能穿过流水线的关键。省掉之后，出端信封里的 `is_encrypt` 会是未定义值，出口解复用就会把结果路由到错误的一侧。这行就是「让 ID 穿越」的那只手。

---

### 4.2 输入端 round-robin 复用

#### 4.2.1 概念说明

有了「信封 + 唯一流水线」，下一个问题是：入口处有加密和解密两个请求方都想往这唯一一条流水线里塞数据，**先服务谁？**

本项目用的是最简单也最公平的策略——**轮询（round-robin）**：用一个 1 比特的状态寄存器 `is_encrypt`，每拍翻转一次。这一拍它指向加密，下一拍指向解密，再下一拍又回到加密……如此交替。被指到的那一侧，如果刚好有有效的请求（`valid=1`）且流水线正好能收（`ready=1`），就完成一次注入；否则这一拍「轮空」，但**指针照样翻**，不补偿、不插队。

这是一种**固定 50/50 的时间片划分**，不考虑谁更「饿」。它的优点是极简（一个翻转触发器 + 一个二选一多路器）、绝对公平；缺点是**不灵活**——即使只有加密有数据、解密完全空闲，加密也只能用到一半的拍子（解密那半个时间片被白白浪费）。

关键术语：

- **轮询仲裁（round-robin arbitration）**：轮流给每个请求方平等的机会。
- **固定时间片（fixed time slot）**：不论是否有需求，每方各占一半时间。
- **吞吐减半（throughput halved）**：共享下每侧峰值吞吐最多是独占时的一半。

#### 4.2.2 核心流程

输入侧调度可以用下面这段伪代码描述（每拍执行一次）：

```
function 输入调度(每拍):
    把流水线输入默认置为无效
    根据当前 is_encrypt 选择数据源：
        if is_encrypt == 1:                       # 本拍轮到加密
            流水线输入.data   = 加密请求.data
            流水线输入.valid = 加密请求.valid
            加密.ready       = 流水线.ready       # 把 ready 回传给加密
        else:                                     # 本拍轮到解密
            流水线输入.data   = 解密请求.data
            流水线输入.valid = 解密请求.valid
            解密.ready       = 流水线.ready
    流水线输入.is_encrypt = is_encrypt            # 给本笔请求盖戳
    is_encrypt = ~is_encrypt                       # 无条件翻转，下一拍换边
```

三条要记住的性质：

1. **翻转是无条件的**：无论本拍有没有真正发生传输（哪怕选中的一侧 `valid=0`），`is_encrypt` 都翻。所以即便只有一侧有连续数据，它也只能隔拍进。
2. **盖戳用的是「当前」的 `is_encrypt`**：这与出口解复用用的「穿过来的 `is_encrypt`」是两回事，隔了 64 拍。
3. **ready 是回传给「当前被选中」的那一侧**：只有轮到的那一侧才能看到流水线的 ready，另一侧的 ready 被置 0。这样不会发生「没轮到的侧误以为能发」。

#### 4.2.3 源码精读

`chacha20_sharing_mux` 是一个被 `#pragma MAIN` 标记的顶层函数（即它自己会被综合成一个独立时钟域的逻辑块）。先看它声明的那四对「虚拟接口」全局线——它们让加解密 FSM「以为」自己各有专属流水线：

[chacha20_pipeline_shared.c:32-39](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20_pipeline_shared.c#L32-L39) —— 八根「虚拟」接口线，对外伪装成两条独立流水线。

```c
stream(chacha20_loop_body_in_t) chacha20_encrypt_pipeline_in;
uint1_t chacha20_encrypt_pipeline_in_ready;
stream(chacha20_loop_body_in_t) chacha20_decrypt_pipeline_in;
uint1_t chacha20_decrypt_pipeline_in_ready;
stream(axis512_t) chacha20_encrypt_pipeline_out;
uint1_t chacha20_encrypt_pipeline_out_ready;
stream(axis512_t) chacha20_decrypt_pipeline_out;
uint1_t chacha20_decrypt_pipeline_out_ready;
```

这些就是加解密 FSM（在 `chacha20.c` 里、`CHACHA_EXCLUDES_PIPELINE` 模式下）会读写的那几根线。本讲的 mux 就是把这几根线接到唯一一条 `chacha20_pipeline` 上。

现在看输入侧调度的核心：

[chacha20_pipeline_shared.c:52-64](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20_pipeline_shared.c#L52-L64) —— round-robin 输入复用。

```c
  // Input side state toggles round robin
  static uint1_t is_encrypt;
  chacha20_pipeline_in.data.is_encrypt = is_encrypt;     // 给本笔盖戳
  if(is_encrypt){
    chacha20_pipeline_in.data.data = chacha20_encrypt_pipeline_in.data;
    chacha20_pipeline_in.valid = chacha20_encrypt_pipeline_in.valid;
    chacha20_encrypt_pipeline_in_ready = chacha20_pipeline_in_ready;   // ready 回传给加密
  }else{
    chacha20_pipeline_in.data.data = chacha20_decrypt_pipeline_in.data;
    chacha20_pipeline_in.valid = chacha20_decrypt_pipeline_in.valid;
    chacha20_decrypt_pipeline_in_ready = chacha20_pipeline_in_ready;   // ready 回传给解密
  }
  is_encrypt = ~is_encrypt;                              // 无条件翻转
```

逐行对照上面的伪代码，可以看到完全一致：`static uint1_t is_encrypt` 是那个翻转状态；第 54 行先把它的值盖到信封的 `is_encrypt` 字段；`if/else` 按它选数据源并把 `valid`、`ready` 接好；第 64 行无条件取反。注意函数开头（第 45-50 行）已经把所有输出默认置 0，所以**没被选中的那一侧** `*_in_ready` 自然保持默认 0，看到「现在轮不到我」。

> 一个容易踩的坑：`is_encrypt = ~is_encrypt` 是**非阻塞式**的翻转吗？在 PipelineC 里，`MAIN` 函数里的 `static` 变量赋值，语义上是「下一拍生效」的寄存器更新（类似 Verilog 时序逻辑里的 `<=`）。所以本拍用的是「旧的」`is_encrypt` 来选择和盖戳，下一拍才用翻转后的新值——这正是我们要的「每拍交替」。

#### 4.2.4 代码实践

**实践目标**：体会「固定 50/50 调度」对吞吐的影响，并验证「单侧有数据时仍只能隔拍进」。

**操作步骤**：

1. 假设加密侧有**连续不断**的请求（每拍 `valid=1`），解密侧完全空闲（每拍 `valid=0`）。在 [chacha20_pipeline_shared.c:52-64](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20_pipeline_shared.c#L52-L64) 上手工「跑」若干拍，逐拍记录 `is_encrypt` 的值和「是否真的有一笔加密请求被注入流水线」。
2. 数一下：每 4 拍里，有几拍真正注入了加密请求？

**需要观察的现象**：

- `is_encrypt` 序列为 1,0,1,0,1,0,…
- 只有 `is_encrypt=1` 的拍，加密的 `valid` 才会被接进流水线；`is_encrypt=0` 的拍，纵然解密 `valid=0`、流水线空转，加密也插不进去。

**预期结果**：每 4 拍只有 2 拍（`is_encrypt=1` 的那两拍）真正注入加密请求——**恰好一半**。这就是「共享导致每侧吞吐减半」的直接来源。即便另一侧完全空闲，固定 round-robin 也不会把空闲的那半个时间片让出来。

> 说明：这是源码追踪型实践。若想看真实波形，可在装好 PipelineC + GHDL + cocotb 的环境里跑 `./build_sim_pipe_shared.sh`（需先 `export PIPELINEC=<PipelineC 可执行路径>`），仿真 250 拍，观察 `chacha20_pipeline_in.valid` 与 `chacha20_pipeline_in.data.is_encrypt` 的关系。**运行结果待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `is_encrypt = ~is_encrypt;` 这一行删掉，会发生什么？

**参考答案**：`is_encrypt` 会永远停在初值（综合后通常为 0），于是调度器**永远只服务解密侧**，加密侧的请求永远得不到注入（`chacha20_encrypt_pipeline_in_ready` 永远是默认 0）。系统看上去像「加密死了」。这说明翻转那行是 round-robin 的心脏。

**练习 2**：相比「固定 50/50 轮询」，如果改成「谁 `valid` 就服务谁、都用 `valid` 时再轮流」（即按需仲裁），对单侧满载的场景有什么好处？为什么本项目仍选了固定轮询？

**参考答案**：按需仲裁下，单侧满载时能用到接近全部拍子，吞吐近 1 块/拍，优于固定轮询的 0.5 块/拍。但按需仲裁需要「记住上次选了谁」并做优先级判断，逻辑更复杂、可能影响时序；而本项目共享的目的是**省面积**，且加解密往往是交替工作的（握手包稀疏），固定 50/50 已够用、实现最简、绝对公平，所以选了它。这是典型的「够用就好」的工程取舍。

---

### 4.3 输出端 ID 解复用与背压

#### 4.3.1 概念说明

请求从入口注入、盖了戳、过完 64 级，现在从出口冒出来了。**怎么把结果还给它真正的主人？** 而且如果主人暂时不想要（`ready=0`），怎么把这个「不想要」顺着流水线**回传**，不让数据丢？

第一件事——**ID 解复用（demultiplexing）**——很简单：出口读「数据自己带出来的那个 `is_encrypt`」（注意，不是入口调度器当前的 `is_encrypt`！），是 1 就把结果送到加密结果线 `chacha20_encrypt_pipeline_out`，是 0 就送到解密结果线 `chacha20_decrypt_pipeline_out`。因为标签是 64 拍前注入时盖的，所以它**精确地对应**「这笔结果当初是谁请求的」。

第二件事——**背压（backpressure）**——更微妙，也是本节的重点。回忆 AXIS 握手：一次传输完成必须 `valid` 和 `ready` 同拍都为 1。在共享场景下，背压要正确地在三个环节之间传递：

- 出口处，目标侧（比如加密）如果 `ready=0`，必须告诉共享流水线「先别把这笔吐给我」→ 即把 `chacha20_pipeline_out_ready` 拉成 0。
- 于是共享流水线这一拍就不推进输出，结果留在流水线末端。
- 这个「停」必须只影响「当前这笔结果」，不能把整条流水线搞乱。

**关键对比（本讲最重要的诚实结论之一）**：在本仓库里，ChaCha20 的共享代码**正确地处理了背压**（ready 双向回传、用 `valid` 门控），而 **Poly1305 的共享代码是一份「未完工」的存档**——它把输入 ready **硬接成 1**、把输出 ready 的回传**注释掉了**。这正是为什么顶层把 Poly1305 共享注释掉、改用每侧独立的 MCP 实例。下面我们把两份代码摆在一起对比。

顺带，本节还会看到**两种「携带 ID」的写法**：ChaCha20 用「手写信封结构体」，Poly1305 用「宏自带 ID 端口」——后者更省事，作者在 ChaCha20 那边还留了 TODO 想换成它。

关键术语：

- **ID 解复用（ID-based demultiplexing）**：按随数据穿出来的标签，把结果分发到对应出口。
- **背压（backpressure）**：下游用 `ready=0` 暂停上游，数据不丢。
- **MCP（Multi-Cycle Path，多周期路径）**：让一段组合逻辑跨多个周期完成、用 `valid`/`ready` 握手，相比深流水线更省面积但吞吐低。Poly1305 实际用的是这个。

#### 4.3.2 核心流程

ChaCha20 的输出解复用 + 背压，伪代码如下：

```
function 输出解复用(每拍):
    默认: 加密结果.valid=0, 解密结果.valid=0, 流水线.out_ready=0
    if 流水线.out.valid:                        # 有结果冒出来
        if 流水线.out.data.is_encrypt == 1:     # 读"穿过来"的标签
            加密结果.data  = 流水线.out.data.data
            加密结果.valid = 1
            流水线.out_ready = 加密结果.ready   # 把目标侧 ready 回传给流水线
        else:
            解密结果.data  = 流水线.out.data.data
            解密结果.valid = 1
            流水线.out_ready = 解密结果.ready
```

注意三点：

1. 解复用的判据是 `流水线.out.data.is_encrypt`——**穿过来的标签**，不是调度器当前的 `is_encrypt`。
2. `流水线.out_ready` 只在「确实有有效结果」时才被赋成「目标侧的 ready」（外面默认是 0）。这保证：目标侧没准备好时，流水线不会丢数据。
3. 输入侧（4.2）和输出侧用的是**同一个** `static is_encrypt` 吗？**不是**。输入侧的 `is_encrypt` 用于「现在轮到谁注入」；输出侧完全不读它，只读穿过来的标签。两者各自独立，相安无事。

#### 4.3.3 源码精读

**ChaCha20 的输出解复用（正确处理背压）**：

[chacha20_pipeline_shared.c:66-77](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20_pipeline_shared.c#L66-L77) —— 按「穿过来的标签」分发，并正确回传 ready。

```c
  // Output side muxing based on id flag out of pipeline
  if(chacha20_pipeline_out.valid){
    if(chacha20_pipeline_out.data.is_encrypt){
      chacha20_encrypt_pipeline_out.data = chacha20_pipeline_out.data.data;
      chacha20_encrypt_pipeline_out.valid = chacha20_pipeline_out.valid;
      chacha20_pipeline_out_ready = chacha20_encrypt_pipeline_out_ready;   // 背压回传
    }else{
      chacha20_decrypt_pipeline_out.data = chacha20_pipeline_out.data.data;
      chacha20_decrypt_pipeline_out.valid = chacha20_pipeline_out.valid;
      chacha20_pipeline_out_ready = chacha20_decrypt_pipeline_out_ready;   // 背压回传
    }
  }
```

对照伪代码完全一致：外层先判 `chacha20_pipeline_out.valid`（有结果才处理），内层按 `data.is_encrypt` 二选一分发，并把**目标侧的 ready 回传成流水线的 `out_ready`**。配合函数开头第 46 行 `chacha20_pipeline_out_ready = 0;` 的默认值，就构成了完整的背压链：目标侧没准备好 → `out_ready=0` → 流水线不吐 → 数据不丢。这是一段**正确、可投产**的共享代码。

**Poly1305 的共享代码（对比：另一种 ID 写法 + 未完工的背压）**。现在看 `poly1305_pipeline_shared.c`。它用的是另一种携带 ID 的手法——不手写信封，而是用一个**自带 ID 端口**的宏：

[poly1305_pipeline_shared.c:11](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_pipeline_shared.c#L11) —— 用宏自动给流水线加 ID 端口。

```c
GLOBAL_PIPELINE_INST_W_VALID_ID(poly1305_mac_compute, u320_t, poly1305_mac_loop_body, poly1305_mac_loop_body_in_t)
```

`GLOBAL_PIPELINE_INST_W_VALID_ID`（with valid + ID）这个宏会自动生成带 `_in_id` / `_out_id` 端口的流水线实例，ID 由专门的并行线携带，不必把 ID 塞进数据结构体。这就是 ChaCha20 那个 TODO 想要的「更省事写法」。调度逻辑里能看到这些带 `_id` 的线：

[poly1305_pipeline_shared.c:35-46](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_pipeline_shared.c#L35-L46) —— Poly1305 的 round-robin 输入复用，但 ready 硬接成 1。

```c
  static uint1_t is_encrypt;
  poly1305_mac_compute_in_id = is_encrypt;                 # 用专门端口写 ID
  if(is_encrypt){
    poly1305_mac_compute_in = poly1305_mac_encrypt_compute_in.data;
    poly1305_mac_compute_in_valid = poly1305_mac_encrypt_compute_in.valid;
    poly1305_mac_encrypt_compute_in_ready = 1; //poly1305_mac_compute_in_ready;   # 硬接 1！
  }else{
    poly1305_mac_compute_in = poly1305_mac_decrypt_compute_in.data;
    poly1305_mac_compute_in_valid = poly1305_mac_decrypt_compute_in.valid;
    poly1305_mac_decrypt_compute_in_ready = 1; //poly1305_mac_compute_in_ready;   # 硬接 1！
  }
  is_encrypt = ~is_encrypt;
```

注意第 40、44 行：`poly1305_mac_encrypt_compute_in_ready = 1;` 和 `poly1305_mac_decrypt_compute_in_ready = 1;`——**输入 ready 被硬接成 1**，而本该用的 `poly1305_mac_compute_in_ready`（注释里那个）被弃用了。这意味着：不管共享流水线到底准不准备好，对上游都永远宣称「我能收」——**这是不安全的**，因为若流水线其实没准备好、上游又信了 `ready=1` 而发数据，数据就可能被吞掉。

再看输出侧：

[poly1305_pipeline_shared.c:28](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_pipeline_shared.c#L28) 与 [poly1305_pipeline_shared.c:49-59](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_pipeline_shared.c#L49-L59) —— 输出 ready 的回传被整段注释掉。

第 28 行 `//poly1305_mac_compute_out_ready = 0; // from compute` 被注释；第 53、57 行的 `//poly1305_mac_compute_out_ready = poly1305_mac_encrypt_compute_out_ready;` 也都被注释。也就是说**出口侧完全没有把目标 ready 回传给共享流水线**——背压链是断的。

把这两点合起来看，结论很清楚：**`poly1305_pipeline_shared.c` 是一份半成品**，背压尚未接通。这正好和顶层那行注释「Poly1305 没那么大，不如不共享、用 MCP 换吞吐」相互印证——作者评估后决定**不投产 Poly1305 共享**，所以这份文件的背压也就没补完。

**那 Poly1305 实际用的是什么？** 看真正被编译的 `poly1305_mac.c`：

[poly1305_mac.c:22-32](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_mac.c#L22-L32) —— 实际生效的是每侧一个 MCP 实例，共享流水线方案被注释。

```c
#ifndef POLY_EXCLUDES_COMPUTE
// Declare poly1305_mac_loop_body compute module to use
// Multi cycle path with valid ready handshake
#include "mcp.h"
GLOBAL_VALID_READY_MCP_INST(PPCAT(POLY_MAC_INST,_compute), u320_t, poly1305_mac_loop_body, poly1305_mac_loop_body_in_t, 4)
// Old pipelined version with just valid bit (not using new stream() types either)
//// TODO can declare as harder to meet timing GLOBAL_FUNCTION that doesnt add IO regs
//#include "global_func_inst.h"
//GLOBAL_PIPELINE_INST_W_VALID_ID(PPCAT(POLY_MAC_INST,_compute), u320_t, poly1305_mac_loop_body, poly1305_mac_loop_body_in_t)
//uint1_t PPCAT(POLY_MAC_INST,_compute_in_ready) = 1;
#endif
```

`encrypt_shared.c` 和 `decrypt_shared.c` 在 `#include "poly1305_mac.c"` 时**没有**定义 `POLY_EXCLUDES_COMPUTE`，所以生效的是 `GLOBAL_VALID_READY_MCP_INST(..., 4)`——**每侧各自一个 4 周期延迟、带 `valid`/`ready` 握手的 MCP 实例**（加密侧 `poly1305_mac_encrypt_compute`、解密侧 `poly1305_mac_decrypt_compute`），各算各的、互不共享。而被注释的第 30 行 `GLOBAL_PIPELINE_INST_W_VALID_ID(...)` 正是「共享流水线方案」的旧痕迹。所以现状是：

> **当前 HEAD：ChaCha20 走共享流水线（生效），Poly1305 走每侧独立 MCP（生效）；Poly1305 的共享流水线方案存在于 `poly1305_pipeline_shared.c` 但未编译、背压未完工。**

这是一条贯穿本节的「诚实线」：README 把两者都描述成共享，但代码的真相是只有 ChaCha20 共享。

#### 4.3.4 代码实践

**实践目标**：对比 ChaCha20 与 Poly1305 两份共享代码的背压处理，亲手找出 Poly1305 版「不安全」的那几行，并理解它为何被弃用。

**操作步骤**：

1. 在 [chacha20_pipeline_shared.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20_pipeline_shared.c) 里，定位输入侧 ready 是怎么写的（应是「回传共享流水线的 ready」），以及输出侧 `chacha20_pipeline_out_ready` 是怎么赋值的（应是「目标侧 ready」）。
2. 在 [poly1305_pipeline_shared.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_pipeline_shared.c) 里，找出三处「与 ChaCha20 不一致」的地方：输入 ready 硬接 1（两行）、输出 ready 被注释（涉及第 28、53、57 行）。
3. 在 [poly1305_mac.c:22-32](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/poly1305/poly1305_mac.c#L22-L32) 里，确认当前生效的是 `GLOBAL_VALID_READY_MCP_INST(..., 4)`，而共享流水线那条 `GLOBAL_PIPELINE_INST_W_VALID_ID(...)` 被注释。

**需要观察的现象**：ChaCha20 的 ready 是「动态回传」的（值随握手变化），Poly1305 的输入 ready 是「常量 1」、输出 ready「不存在」。

**预期结果**：你能用一句话说清——「ChaCha20 共享是**吞吐换面积**且**握手正确**的可投产设计；Poly1305 共享是一份**未接通背压**的存档，故被顶层注释、改用每侧独立 MCP 以保留各自的全速吞吐」。

> 说明：以上为源码阅读型实践，无需运行即可完成。

#### 4.3.5 小练习与答案

**练习 1**：在 ChaCha20 的输出解复用里，为什么判据是 `chacha20_pipeline_out.data.is_encrypt`，而不能直接用输入侧那个 `static is_encrypt`？

**参考答案**：因为两者差了 64 拍。输入侧的 `is_encrypt` 表示「**现在**轮到谁注入」；而当前冒出来的这笔结果是 64 拍前注入的，它的主人由「**当时**盖的戳」决定，也就是随它一路穿出来的 `data.is_encrypt`。若误用输入侧当前的 `is_encrypt`，就会把结果路由给「现在轮到」的那一侧——与这笔结果真正的主人无关，会错配。

**练习 2**：Poly1305 共享版如果把输入 ready 硬接成 1，具体会在什么情形下丢数据？

**参考答案**：当 round-robin 轮到某一侧、该侧恰好有 `valid=1` 的请求，但共享的 `poly1305_mac_compute` 其实还没准备好接收（`poly1305_mac_compute_in_ready` 实际为 0）时。因为代码对上游硬报 `ready=1`，上游会以为「被收下了」而推进自己的状态机/丢弃这笔数据，但共享流水线并没有真正收下它——数据就丢了。这就是「背压断链」的危险。

**练习 3**：既然 Poly1305 改用了每侧独立 MCP，加密和解密两侧的 Poly1305 还会互相抢资源吗？吞吐上相比「共享一条」各是多少？

**参考答案**：**不会抢**。两侧各有一个独立的 `poly1305_mac_encrypt_compute` / `poly1305_mac_decrypt_compute`，并行工作、互不影响，每侧都能用到 MCP 的满速（4 周期出一块），**吞吐高于「共享一条」时各占一半**。代价是面积翻倍——这正是顶层注释里「用 MCP 换吞吐」的含义：Poly1305 的计算逻辑没大到值得为它忍受共享的吞吐减半，不如各买一份求全速。

---

## 5. 综合实践

**任务**：描述「加密与解密请求**同时**到达共享 ChaCha20 流水线」时的完整调度顺序与结果路由过程。

这是一个贯穿本讲三个模块的串联练习，请你结合源码画出从「两个请求同时到达」到「两个结果各自回到正确的主人」的全过程。

**背景设定**：

- 设第 `T` 拍，加密侧和解密侧**同时**各有一个有效的 512 位块请求到达虚拟接口：`chacha20_encrypt_pipeline_in.valid=1` 且 `chacha20_decrypt_pipeline_in.valid=1`。
- 设进入调度器时 `static is_encrypt` 的当前值为 `1`（即本拍轮到加密）。
- 设流水线与上下游全程 `ready=1`（不考虑背压停顿），流水线深度为 64 拍。
- 共享流水线输入端只接受一个请求/拍。

**请完成**：

1. **注入顺序**。逐拍写出第 `T`、`T+1` 拍：哪一侧的请求先被注入共享流水线？盖的戳分别是多少？另一侧在第几拍才被注入？（提示：看 [chacha20_pipeline_shared.c:52-64](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20_pipeline_shared.c#L52-L64)，注意 `is_encrypt` 是注入**之后**才翻转的。）
2. **穿越**。两个请求注入后，各自携带的 `is_encrypt` 标签会经历什么？经过多少拍到达出口？
3. **路由**。在出口处，先到达的结果携带的 `is_encrypt` 是多少？应被路由到 `chacha20_encrypt_pipeline_out` 还是 `chacha20_decrypt_pipeline_out`？后到达的呢？（提示：看 [chacha20_pipeline_shared.c:66-77](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/pipelinec_build/src/chacha20/chacha20_pipeline_shared.c#L66-L77)。）
4. **画一张时序简图**：横轴为拍（`T` 到 `T+66` 左右），画出 `is_encrypt`（输入侧）、注入事件、出口 `valid` 与出口 `data.is_encrypt`、两路结果的去向。

**参考答案**：

1. **注入顺序**：
   - 第 `T` 拍：`is_encrypt=1`（轮到加密）→ 加密请求被注入，盖戳 `is_encrypt=1`；本拍结束后 `is_encrypt` 翻成 0。解密请求本拍**没轮上**（它对应的 `chacha20_decrypt_pipeline_in_ready` 是默认 0）。
   - 第 `T+1` 拍：`is_encrypt=0`（轮到解密）→ 解密请求被注入，盖戳 `is_encrypt=0`；本拍结束后 `is_encrypt` 翻回 1。
   - 所以**加密先、解密后**，相差 1 拍注入。这正是 round-robin 的固定交替。
2. **穿越**：两个信封各自带着自己的戳（一个 1、一个 0），随数据穿过 64 级 `chacha20_pipeline`，期间计算 ChaCha20 块，标签原样不动。加密请求在第 `T+64` 拍到达出口，解密请求在第 `T+65` 拍到达出口（相差 1 拍，与注入间隔一致）。
3. **路由**：
   - 第 `T+64` 拍：出口 `valid=1`，读 `data.is_encrypt=1` → 路由到 **`chacha20_encrypt_pipeline_out`**（还给它真正的主人：加密）。
   - 第 `T+65` 拍：出口 `valid=1`，读 `data.is_encrypt=0` → 路由到 **`chacha20_decrypt_pipeline_out`**（还给它真正的主人：解密）。
   - 关键点：出口判据是「**穿过来的标签**」，所以即便输入侧的 `is_encrypt` 在这 64 拍里已经翻了 64 次，路由仍然 100% 正确。
4. **时序简图（示意）**：

   ```
   拍:        T    T+1   T+2  ...  T+64  T+65  T+66
   ----------------------------------------------------
   in is_encrypt(选): 1    0    1        0     1     ...   (每拍翻)
   注入事件:        ENC  DEC  (空/...) 
   注入盖戳:         1    0
                                    ...  (64 拍穿越) ...
   out valid:                            1     1
   out data.is_encrypt:                  1     0
   结果去向:                         →ENC  →DEC
   ```

   可以看到：**注入顺序（ENC→DEC）与还回顺序（ENC→DEC）一致**，因为 FIFO 式的流水线保序；标签是「正确还回主人」的唯一依据。

**延伸思考（选做）**：如果把背景设定改成「`is_encrypt` 当前为 0」，整个时序会怎样平移？（答：解密先注入、加密后注入，出口顺序也随之反过来，但「先注入先出来、按标签路由」的性质不变。）

> 说明：本实践为源码追踪 + 手工推演型，无需运行工具链。若要在仿真中亲眼验证「注入顺序与出口标签」，可跑 `./build_sim_comb_shared.sh`（组合仿真，75 拍）或 `./build_sim_pipe_shared.sh`（流水线仿真，250 拍），需要预装 PipelineC/GHDL/cocotb 并 `export PIPELINEC=...`。**运行结果待本地验证**。

## 6. 本讲小结

- **共享 = 时分复用一份昂贵硬件**：加密和解密的 ChaCha20 计算逻辑完全相同，于是只造一条 64 级流水线让两者轮流用，省下近一半面积，代价是每侧峰值吞吐减半（「以吞吐换面积」）。
- **核心机制是「ID 标签穿越流水线」**：请求注入时盖 1 比特戳 `is_encrypt`，作为数据的一部分随它穿过全部 64 级；出口再按这个**穿过来的**标签把结果路由回正确的主人——出口从不依赖入口调度器当前的状态。
- **入口是固定 50/50 的 round-robin**：一个 `static uint1_t is_encrypt` 每拍无条件翻转，轮流给加密/解密各一拍时间片；单侧满载时也只能用到一半拍子，公平但不灵活。
- **ChaCha20 共享正确处理了背压**：输入侧把共享流水线的 ready 回传给当前被选中的一侧，输出侧把目标侧的 ready 回传给共享流水线，配合 `valid` 门控，数据不会丢——这是一段可投产的代码。
- **Poly1305 共享是一份「未完工」存档**：它演示了另一种更省事的 ID 写法（`GLOBAL_PIPELINE_INST_W_VALID_ID` 宏自带 ID 端口），但输入 ready 被硬接成 1、输出 ready 回传被注释，背压链是断的。
- **当前 HEAD 的真相**：顶层 `chacha20poly1305_encrypt_decrypt_shared.c` 只把 ChaCha20 共享 `#include` 进来，Poly1305 共享被注释掉；Poly1305 实际改用每侧独立的 MCP（`GLOBAL_VALID_READY_MCP_INST(..., 4)`）以保留全速吞吐——尽管 README 把两者都描述成共享。读源码时永远以**实际编译的代码**为准。

## 7. 下一步学习建议

- **若想看 Poly1305 的「另一条路」如何被正确实现**：本讲只讲了 Poly1305 共享的存档与独立 MCP。建议接着读 u5-l6（Pypeline Python 前端），看 Python 重写版如何修复 C 版 Poly1305 的 320 位 limb 数学 bug，并理解为什么 Poly1305 的正确性远比「是否共享」更关键。
- **若想验证共享设计的端到端行为**：在具备 PipelineC + GHDL + cocotb 的环境里运行 `./build_sim_pipe_shared.sh`，观察 `chacha20_pipeline_in.data.is_encrypt` 的注入序列与 `chacha20_pipeline_out.data.is_encrypt` 的出口序列，亲手印证本讲综合实践的推演。
- **若想回到 SoC 视角**：这套共享加密核目前**尚未**被焊进 `top.filelist`（见 u5-l2）。理解它如何有朝一日接入 DPE 的 `dpe_wg_encryptor` / `dpe_wg_decryptor`（u4-l5），需要结合 cryptokey_table（u4-l6）的 B 口如何把 key/nonce 喂给加解密 FSM。建议复习 u4-l5 与 u4-l6，把「数据面如何驱动加密核」补全。
- **关于「以吞吐换面积」的更广视角**：本讲的 round-robin 共享是硬件设计里的经典手法。可以把它和 u4-l2（DPE 多路复用器的按包轮询）对照阅读——两者都是「一份资源服务多个请求方」，只不过一个在包级别、一个在密码块级别。
