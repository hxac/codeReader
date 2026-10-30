# 固定内存、流同步与 #704 教训

## 1. 本讲目标

解码路径上有一个毫不起眼却上过事故报告的动作:把 GPU 上刚算出的 token id 拷回主机内存。本讲围绕这一个动作,讲透三件事:

1. **pageable vs pinned**:为什么一次对普通(可换页)主机内存的 D2H 回读带有同步拷贝语义,以及它如何在与 KV 恢复大块拷贝流量相撞时,把一个亚毫秒调用拖成 23.6 ms、冻结所有活跃流的 token 交付(事故编号 #704)。
2. **write-combined 页的告诫**:`SampleScratch` 的两个主机落地缓冲由 `unsafe alloc_pinned` 分配,cudarc 把它们硬编码为 write-combined 页——CPU 读这种页不经过缓存,因此「只准落完再读、不准扩成热点读取结构」。
3. **`stream_spin_wait` 的事件级等待**:`select_batch` 贪心分支那行 `stream_spin_wait` 前的注释为什么说「等这个拷贝的事件传递覆盖了同流上更早排队的 argmax 核」,以及自旋等待如何把调度器唤醒延迟从步骤循环里拿掉。

学完本讲,你应该能独立画出 #704 事故与修复后两种方案的时序图,并能论证那行注释里的传递覆盖逻辑。

## 2. 前置知识

本讲是全手册里最偏「系统底层」的一讲,先用通俗语言铺垫四个概念。已在 u2-l2 见过 pinned 一词的读者,可以在这里补上它背后的事故级细节。

### 2.1 pageable(可换页)内存与 DMA

CPU 上的普通内存由操作系统按「页」管理,虚拟地址到物理地址的映射随时可能改变(换页、迁移、压缩)。而 GPU 和 CPU 之间只隔着一条 PCIe 总线,真正搬数据的是 **DMA 引擎**——它只认物理地址,并且要求搬运期间物理地址不变。

这就有了一个根本矛盾:**驱动不敢把一个「随时可能被操作系统挪走」的页直接交给 DMA**。CUDA 运行时(CUDA Programming Guide 所载的通用行为)对落在 pageable 内存上的拷贝只能采取保守做法:由驱动经内部锁定的暂存页中转,或临时锁页,调用线程要等数据真正落到用户页才返回。表现出来就是**同步拷贝语义**——尤其 D2H 方向:数据到达主机侧时必须写进用户的 pageable 页,这一段无法提前异步完成。

### 2.2 pinned(页锁定)内存

pinned 内存(页锁定内存,`cuMemAllocHost` / cudaMallocHost 分配)在分配时就向操作系统申请:这些页的物理地址永久固定、不可换出。于是 DMA 引擎可以直接、**异步**地向它搬运——`cuMemcpyAsync` 把拷贝排进流队列就立刻返回,真正完成靠之后的事件或流同步确认。

一句话对比:

| | pageable 页 | pinned 页 |
|---|---|---|
| 物理地址 | 可变 | 锁定不变 |
| D2H 拷贝语义 | 同步(调用线程等) | 异步入队即返回 |
| 等待时长 | 取决于**通道上所有排队者** | 只取决于这次拷贝自身 |
| 分配/释放成本 | 廉价 | 较贵(向 OS 锁页) |

### 2.3 CUDA 流与按序执行

同一条 CUDA 流上的工作(内核发射、拷贝)**严格按入队顺序执行**。这是本讲传递覆盖论证的基石:如果拷贝 B 排在内核 A 之后入队,那么「B 已完成」必然蕴含「A 也已完成」。

### 2.4 write-combined(写合并)页

write-combined(WC)是一种特殊的 pinned 页:CPU 对它的写会被合并成整条缓存行一次突发发给 PCIe(写效率极高),但**CPU 侧不缓存它的内容,读它要直接拖内存,很慢**。这种页为「CPU 只写、设备只读」的单向数据流设计。反方向用(让 CPU 频繁读)就是拿缓存行换慢速内存访问。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
|---|---|
| `pegainfer-sample/src/lib.rs` | 全部主素材:`argmax_host`/`argmax_host_alt` 字段注释(#704 叙事原始出处)、`alloc_pinned` 调用与 write-combined 告诫、`select_batch` 贪心分支的回读与等待、`greedy_collect_resident` 的单事件等待 |
| `pegainfer-kernels/src/tensor.rs` | `stream_spin_wait` 的自旋实现、`active_cu_stream` 的有效流解析 |
| `pegainfer-kv-offload/src/engine.rs` | #704 的「另一侧」:KV 恢复 bulk 拷贝的后端选择注释 |
| `pegainfer-kv-cache/tests/restore_commit.rs` | #704 的注册成本回归锚点(36 ms → ~1.6 ms) |

本讲交叉引用少量前讲结论:u2-l2 讲过 `SampleScratch` 的整体缓冲布局,u4-l1 讲过分阶段贪心四件套与 base-stream-only 防护。这里只取用、不重述。

## 4. 核心概念与源码讲解

### 4.1 pageable vs pinned D2H:#704 事故解剖

#### 4.1.1 概念说明

`select_batch` 的贪心分支最后必须把 `argmax_out`(设备上的 token id 数组)拷回主机,才能把 `Vec<u32>` 交给上层。这次回读本身只有几十到几百字节,亚毫秒就能完成——**前提是它真的是一次异步小拷贝**。

#704 之前,落地区是一块 pageable 主机内存。按 2.1 节的通用行为,对 pageable 页的 D2H 带同步拷贝语义:步骤线程发起回读后要站在原地等驱动把数据亲手放进用户页。等待的时长不再由「我拷多少字节」决定,而由「拷贝通道上排在我前面的还有谁」决定。

恰好在某些部署里,请求的 KV 缓存恢复(从主机/SSD offload 层回到 GPU)会产生**大块拷贝洪流**。代码注释记载的原始表述是「a P/D KV-restore flood」。洪流占住拷贝通道时,那个亚毫秒的回读被拖成 **23.6 ms**——而 `select_batch` 位于解码步骤循环的同步点上,一次回读卡住,意味着这一步的所有 token 都发不出去,**每一条活跃流的 token 交付同时冻结**。

修复:落地区改为 `alloc_pinned` 得到的固定页。D2H 对 pinned 页是异步的(入队即返回),之后读取方只阻塞在**这次拷贝自己的完成事件**上——通道里有没有别人排队,与它无关。

#### 4.1.2 核心流程

事故形态(pageable 落地区)的文字时序图:

```text
步骤线程                 DMA/拷贝通道                     恢复线程
   │                        │                              │
   │                        │◄──── KV 恢复大块拷贝入队 ──────│
   │                        │      (bulk restore 洪流)      │
   │  argmax 完成           │                              │
   │  发起 pageable D2H ────► 排在洪流之后                  │
   │  【同步语义:原地等待】  │ ...搬运洪流(数十 ms)...      │
   │  等待...               │ ...搬运洪流...                │
   │  等待...总计 23.6ms     │◄── 终于轮到这次小回读 ────────  │
   │  拿到 token             │                              │
   │  (所有活跃流的 token     │                              │
   │   交付被冻结 23.6ms)    │                              │
```

修复形态(pinned 落地区):

```text
步骤线程                 DMA/拷贝通道                     恢复线程
   │                        │                              │
   │                        │◄──── KV 恢复大块拷贝入队 ──────│
   │  argmax 完成           │                              │
   │  发起 pinned D2H ──────► 排队(异步,立即返回)         │
   │  (继续做别的事)         │ ...洪流与小回读并行调度...     │
   │  等待仅本次拷贝的事件    │                              │
   │  ←拷贝自身完成即返回     │                              │
   │  拿到 token(亚毫秒)    │                              │
```

两者等待时长的差别可以写成:

\[ t_{\text{pageable}} \approx t_{\text{前车}} + t_{\text{自身}} \qquad t_{\text{pinned}} \approx t_{\text{自身}} \]

其中 \( t_{\text{前车}} \) 是拷贝通道上先前排队者的剩余时长——#704 里它比 \( t_{\text{自身}} \) 大两到三个数量级。

#### 4.1.3 源码精读

**#704 叙事的原始出处**——`SampleScratch` 的字段文档注释,把事故、数字、后果、修法完整冻结在代码里:

[src/lib.rs:71-79](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L71-L79) —— `argmax_host` 字段注释:pageable 回读的同步拷贝语义会让步骤线程排在并发 bulk 拷贝之后;一次 P/D KV-restore 洪流把这个亚毫秒调用拖平到 23.6 ms 并冻结所有活跃流的 token 交付(#704);pinned 保持 D2H 异步,读取方只阻塞在拷贝自己的事件上。`argmax_host_alt` 是收集上一次回读期间使用的交替落地槽(双槽轮转,u4-l1 已详述)。

**修复的落地**——构造函数里的两处 `unsafe alloc_pinned`:

[src/lib.rs:134-141](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L134-L141) —— 两个落地槽都来自 `ctx.ctx.alloc_pinned::<i32>(max_rows)`,即页锁定内存。`unsafe` 提醒你:绕过了 Rust 的分配器,换来了 DMA 可直接异步写入的物理稳定性。

**#704 的另一侧(KV 恢复线程)**——恢复洪流本身在事故后也被调优过:

[pegainfer-kv-offload/src/engine.rs:172-183](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kv-offload/src/engine.rs#L172-L183) —— 注册传输层时默认选 Direct 后端(`cuMemcpyAsync` 走 DMA 引擎):Kernel 后端在 #704 的 fragmented bulk-restore 批上 A/B 过,对共驻解码更差(它的网格跨步拷贝核会与解码核抢 SM)。同一段事故牵出的两侧修复:采样侧换 pinned 落地区,恢复侧挑不抢 SM 的传输后端。

[pegainfer-kv-cache/tests/restore_commit.rs:1-5](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kv-cache/tests/restore_commit.rs#L1-L5) —— #704 的注册成本回归锚点:1024 块 commit 在 registry PRT 修复前 36 ms、修复后约 1.6 ms。注意这些数字是**恢复侧注册**的成本,与采样侧注释里的 23.6 ms 是同一次事故的两条战线。

#### 4.1.4 代码实践

**实践目标**:不运行任何代码,凭 2.1/2.2 节的概念与上述注释,亲手复现 #704 的因果链,检验自己是否真的理解了同步语义的来源。

**操作步骤**:

1. 打开 [src/lib.rs:71-79](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L71-L79),把注释里的事实逐条抄成清单(至少应含:同步拷贝语义、KV-restore 洪流、23.6 ms、冻结所有活跃流、pinned 修法)。
2. 在纸上画 4.1.2 节那样的两张时序图(先遮住答案自己画,画完对照)。
3. 在事故图上标出三个问题各自的位置:(a) 谁占着拷贝通道;(b) 步骤线程在等谁;(c) 哪个数字是 23.6 ms 的组成部分。

**需要观察的现象**:纯纸笔实践,无可运行现象。

**预期结果**:你能不回头看书地回答——「pageable D2H 的问题不是慢,是**等待时长不可控**,因为它继承了通道上所有前车的延迟;pinned 的问题边界是这次拷贝本身」。若答不出,回到 2.1 节重读驱动为何不敢把可换页地址交给 DMA。

#### 4.1.5 小练习与答案

**练习 1**:KV 恢复的 bulk 拷贝方向是主机→设备(H2D),回读是设备→主机(D2H),方向相反,为什么 H2D 洪流能拖慢 D2H 回读?

**答案**:它们共享同一批资源——拷贝通道(DMA 引擎/驱动内的暂存机制)与驱动锁。pageable D2H 的同步语义要求调用线程等驱动完成「经暂存中转、写进用户页」的全过程,而驱动/通道正被 bulk 洪流占着,回读只能排队。方向不同不等于通道不同。

**练习 2**:如果把 `argmax_host` 换回 `Vec<i32>`(pageable),并在 `memcpy_dtoh` 后立即读取,在不发生 KV 恢复的安静机器上大概率测不出问题。这样的「修复回退」为什么仍然不可接受?

**答案**:因为 pageable 回读的等待时长由**环境**决定而非由代码决定——本讲引用的注释 freezing 场景正是部署期才出现的洪流。安静机器上测不出,恰是这类 bug 的危险之处:它是一个随负载出现的延迟炸弹,而不是一个稳定的错误。

**练习 3**:23.6 ms 冻结的是「每一个活跃流的 token 交付」,而卡住的只是采样这一个环节。为什么波及面是全部活跃流?

**答案**:`select_batch` 在解码步骤循环的同步点上,一批里所有行的 token 都要等这次回读落地才能散写返回;解码又是所有流共用的步骤节奏,一次回读卡住即整步卡住,每条流的下一个 token 都发不出去(见 [src/lib.rs:248-262](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L248-L262) 的批式结构:一次回读服务全部贪心行)。

### 4.2 write-combined 页:便宜的不是白给的

#### 4.2.1 概念说明

`alloc_pinned` 拿到的固定页还有第二个属性:本项目的注释明确写着「cudarc's alloc_pinned hardcodes write-combined memory, whose CPU reads are uncached」——cudarc 的 `alloc_pinned` 硬编码分配 write-combined 页,CPU 读它不经过缓存(见 2.4 节)。

为什么这里可以接受?算一笔账:落地槽装的是 `max_rows` 个 `i32`——解码槽位最多 16 行时只有 **64 字节**。解码每步对它做一次顺序读、每元素只读一次、读完即弃。这种「一次顺序扫」的访问模式,缓存本来就没帮多少忙。

注释接着给出告诫:**「fine for max_rows i32s, but don't grow this buffer into anything read in a hot loop」**——不要把这个缓冲扩展成任何在热点循环里反复读的结构。例如,若有人想「省一次设备拷贝」,把整行 top-k 值、甚至整段 logits 都落到这种页里再反复扫描,每次扫描都在付 uncached 读的代价,等于把快速显存换成了慢速内存。

此外还有一条**正确性**纪律:「Read only after a D2H lands in it (write-combined pages start uninitialized)」——这种页初始内容未定义(对比设备侧 `alloc_zeros` 会清零,pinned 分配不清零),且 D2H 是异步入队的:拷贝没落地之前读它,读到的要么是垃圾、要么是上一代 token。所以读取必须发生在「确认这次拷贝完成之后」——这正是 4.3 节等待原语的职责。

#### 4.2.2 核心流程

落地槽的生命周期纪律:

```text
alloc_pinned(max_rows)          ← 分配,内容未定义,不清零
        │
        ├── D2H 异步入队(memcpy_dtoh 到 argmax_host / _alt)
        │        └─ 排在 argmax 核之后,同流按序执行
        │
        ├── 【等待:流排空 / 拷贝事件完成】← 唯一合法的读取时机分界线
        │
        └── as_slice() 顺序读一次 → 散写进 tokens 向量 → 本步结束
（下一步换另一个槽,双槽轮转,u4-l1)
```

要点:分配一次、终身复用(CUDA 图指针稳定);每步的读只做一次;读的量以「几十字节」计。

#### 4.2.3 源码精读

[src/lib.rs:134-141](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L134-L141) —— 注释三连:只在 D2H 落地后读(初始未定义);alloc_pinned 硬编码 write-combined、CPU 读 uncached;对 `max_rows` 个 i32 无妨,但**不要**把该缓冲扩成热点循环读取的结构。紧随其后的两行 `unsafe { ctx.ctx.alloc_pinned::<i32>(max_rows) }` 分配主槽与交替槽。

[src/lib.rs:256-262](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L256-L262) —— 合法读取的样子:`as_slice()` 拿到切片后,`for (k, &row) in greedy.iter().enumerate()` 顺序扫一遍、按下标散写进 `tokens`,没有任何重读。这就是「一次顺序扫」的标准形态。

#### 4.2.4 代码实践

**实践目标**:把「多大算安全、多大算违规」变成你自己的量化直觉。

**操作步骤**:

1. 取 Qwen3 的词表宽度 151936 与解码槽位数 16,分别计算:(a) 落地槽 `max_rows` 个 i32 的字节数;(b) 假设有人把整行 logits(bf16)也落进 write-combined 页的字节数。
2. 对照现代 CPU 一级缓存行 64 字节:算 (a) 覆盖几条缓存行、(b) 覆盖多少条。
3. 写下结论:哪个访问模式在 uncached 页上损失可忽略,哪个会把解码每步都拖慢。

**需要观察的现象**:纯计算,无可运行现象。

**预期结果**:(a) 16 × 4 B = 64 B,恰好一条缓存行量级,一次顺序读损失可忽略;(b) 151936 × 2 B ≈ 296 KB,若在热点循环里反复扫,相当于每步都从内存裸拖 296 KB,缓存完全不帮忙——这正是注释禁止的形态。另注意 (b) 还叠加了「每步读 16 行」的情形,损失再乘 16。

#### 4.2.5 小练习与答案

**练习 1**:write-combined 页的「写合并」对 D2H 落地槽有什么用?好像设备写、CPU 读,方向反了?

**答案**:写合并优化的是 CPU→PCIe 方向的写(H2D);对 D2H 落地槽而言它没有正向收益,只是 cudarc `alloc_pinned` 统一硬编码的属性。本项目接受它是因为读取模式(一次顺序扫几十字节)对缓存不敏感——收益来自 pinned 的异步 D2H,write-combined 是搭着来的代价。

**练习 2**:「只在 D2H 落地后读」这条纪律,违反它是性能问题还是正确性问题?

**答案**:正确性问题。两条:其一,write-combined 页初始内容未定义,首次使用前读它是读垃圾;其二,D2H 异步入队后拷贝尚未完成,提前读会读到上一代 token——采样结果错误且很可能不报错。性能问题(uncached 读)是另一层,量级小得多。

**练习 3**:为什么不干脆用「可缓存的 pinned 页」(CUDA 允许分配带 host-register 缓存属性的固定内存)两全其美?

**答案**:技术上存在这种选择,但 cudarc 的 `alloc_pinned` 没有暴露该选项(注释明说 hardcodes write-combined),而本项目的读取模式(64 B、一次顺序扫)让缓存属性几乎不影响性能。为一个不存在的收益去绕开或替换底层分配器,违反本 crate「策略层薄、原语归 kernels」的分层纪律——不如把告诫写进注释,守住「不扩成热点读取结构」这条使用纪律。(「cudarc 未暴露」以注释为准;若未来版本暴露,这也只是一个可评估的优化项,不是正确性修复。)

### 4.3 stream_spin_wait:自旋、事件与传递覆盖

#### 4.3.1 概念说明

pinned 解决了「等待时长不可控」,剩下的问题是「怎么等」。CUDA 提供一串粒度递减的等待方式:

| 等待方式 | 粒度 | 线程行为 |
|---|---|---|
| `ctx.sync()`(设备同步) | 整个设备所有流 | 阻塞,线程让出 CPU |
| `cuStreamSynchronize`(流同步) | 一条流 | 阻塞,线程让出 CPU |
| 轮询 `cuStreamQuery` 直到 SUCCESS | 一条流 | **自旋忙等**,响应即时的同时烧一个核 |

解码步骤循环里,每次等待的对象通常是**微秒级**的拷贝。阻塞式同步要让线程睡眠再被调度器唤醒,唤醒延迟(微秒到几十微秒量级)可能比等待本身还贵,而且每步都付一次。所以本项目实现了 `stream_spin_wait`:先自旋轮询最多 5 ms(解码场景下实际等待远短于这个上限),超时才退回阻塞式 `cuStreamSynchronize` 兜底。`select_batch` 贪心分支注释的原话:「The spin ahead of it takes the scheduler wake-up out of the step loop」——自旋把调度器唤醒从步骤循环里拿掉了。

再看那行注释的前半句,本讲最核心的论证:

> Blocks on this copy's own event — which transitively covers the argmax kernel queued before it on the same stream, so the wait is equivalent to the old full-stream sync for this path.

翻译:等待挂在这个拷贝自己的完成上——它**传递覆盖**(transitively covers)了同流上排在它之前入队的 argmax 核,因此对这条路径而言,这一次等待等价于旧版的全流同步。

论证分三步:

1. **入队顺序**:贪心分支在基流上依次入队 H2D 行号表 → argmax 核 → D2H 回读(见 u2-l3 / [src/lib.rs:235-250](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L235-L250))。
2. **同流按序执行**(2.3 节):回读完成 ⇒ 排在它之前的 argmax 必然已完成 ⇒ 更早的 H2D 也已完成。
3. **等价性**:由于回读是这次等待涉及的最后入队者,「等它完成」与「等整条流排空」在这条路径上是同一件事——于是旧代码里那次更重的整流(或全设备)同步可以被这一次针对最后工作的等待替代,正确性分毫不差。

还有一个实现层面的精确化,读源码时必须看清:`stream_spin_wait` 轮询的不是单个事件句柄,而是用 `cuStreamQuery` 问「整条 active 流排空了没」。注释说的「this copy's own event」是在陈述等价关系(该拷贝是流上最后的工作,流排空 ⇔ 该拷贝完成),不是字面上等一个 event 对象。真正字面意义的「只阻塞在拷贝自身事件」出现在 u4-l1 的分阶段路径:`greedy_collect_resident` 里的 `as_slice()` 只等落进这个 pinned 缓冲的那次拷贝,流上后续别的工作照跑——那是更细的一档粒度,服务于「先发射第 N+1 步、再收集第 N 步」的流水。

#### 4.3.2 核心流程

`select_batch` 贪心分支的完整等待链:

```text
1. memcpy_htod(greedy 行号 → row_indices)     [入队]
2. argmax_batch_bf16_split_indexed_into(...)   [入队:内核]
3. memcpy_dtoh(argmax_out → argmax_host)       [入队:异步 D2H,pinned]
4. stream_spin_wait(ctx):
     deadline = now + 5ms
     loop { cuStreamQuery(active stream)
            SUCCESS      → 返回          ← 流排空 ⇔ 第 3 步完成
            NOT_READY    → 未到 deadline:spin_loop 继续
                          → 到 deadline:  cuStreamSynchronize 兜底 }
5. argmax_host.as_slice()                       [此刻保证数据已落地]
6. 散写 tokens[row] = out[k]                     [一次顺序扫]
```

传递覆盖链:第 4 步返回 ⇒ 第 3 步完成 ⇒(同流按序)第 2 步完成 ⇒ 第 1 步完成。一次等待,覆盖三件前序工作。

#### 4.3.3 源码精读

**等待发生处与注释原文**:

[src/lib.rs:248-255](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L248-L255) —— `memcpy_dtoh` 把 `argmax_out` 异步拷进 pinned 槽 `argmax_host`;紧随的注释就是本讲反复引用的传递覆盖论证与「自旋拿掉调度器唤醒」的动机,下一行 `stream_spin_wait(ctx)?` 执行等待。

**自旋等待的实现**:

[pegainfer-kernels/src/tensor.rs:82-105](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L82-L105) —— `stream_spin_wait`:取 `active_cu_stream` 的句柄,以 `now + 5ms` 为上限循环 `cuStreamQuery`;`CUDA_SUCCESS` 即返回,`CUDA_ERROR_NOT_READY` 时未到上限就 `spin_loop()`(给 CPU 的提示:这是自旋等待),到上限退回 `cuStreamSynchronize` 阻塞兜底并检查返回值。常数 `STREAM_SPIN_WAIT_CAP` 就定义在上方([pegainfer-kernels/src/tensor.rs:83](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L83))。

**它问的是哪条流**:

[pegainfer-kernels/src/tensor.rs:73-80](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L73-L80) —— `active_cu_stream`:线程局部 override 有值时返回副流,否则返回 `ctx.stream`(基流)。`select_batch` 的 D2H 固定走 `ctx.stream`(基流)入队,而 `stream_spin_wait` 等的是 active 流——两者何时会不一致,见练习 3。

**细粒度对照(不重复 u4-l1,只看等待语义)**:

[src/lib.rs:412-434](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L412-L434) —— `greedy_collect_resident`:不碰流、不发射任何工作,直接 `as_slice()` 取数——按字段注释([src/lib.rs:75-76](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L75-L76))读取方只阻塞在拷贝自身的事件上。与 `select_batch` 的「自旋等整条流」对照,这正是两档等待粒度:批式路径简单直接,分阶段路径为流水让路。

**粗粒度对照**:

[src/lib.rs:618-634](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L618-L634) —— `token_logprobs_batch` 用四次 `clone_dtoh` 加一次 `ctx.sync()`。为什么那里敢用全设备同步?u3-l2 讲过:logprob 是按需路径,只在请求方显式要求时运行,不在解码步骤循环上,付得起粗粒度同步的代价。三档粒度在同一个文件里各就各位,选择标准是「调用频率 × 所在路径」。

#### 4.3.4 代码实践

**实践目标**:把「每处等待的粒度与理由」从源码里挖出来,形成一张等待点清单——这是读懂任何 CUDA 异步代码的基本功。

**操作步骤**:

1. 在 `src/lib.rs` 里找出全部同步/等待点(提示:搜索 `sync`、`stream_spin_wait`、`as_slice`,至少能找到四处:`select_batch` 贪心分支、`greedy_collect_resident`、`token_logprobs_batch` 的 `ctx.sync()`、以及各入口的 `has_stream_override` 拒绝)。
2. 为每一处登记四项:等待原语、粒度(设备/流/单事件)、所在路径(解码步骤循环内/外)、选择理由。
3. 单独复核 4.3.2 节的传递覆盖链:对着 [src/lib.rs:235-255](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L235-L255) 确认入队顺序确为 H2D → argmax → D2H,然后用自己的话写下为什么第 4 步的一次等待覆盖了三件前序工作。

**需要观察的现象**:纯源码阅读,无可运行现象。

**预期结果**:一张类似下表的清单(你的表述可以不同,粒度与理由必须对):

| 位置 | 原语 | 粒度 | 路径 | 理由 |
|---|---|---|---|---|
| `select_batch` 贪心分支 | `stream_spin_wait` | active 流(自旋 ≤5ms) | 解码步骤循环内 | 微秒级等待避开调度器唤醒;传递覆盖前序工作 |
| `greedy_collect_resident` | `as_slice` | 单次拷贝 | 解码步骤循环内(分阶段) | 给 N+1/N 双槽流水让路 |
| `token_logprobs_batch` | `ctx.sync` | 整设备 | 按需路径 | 频率低,粗粒度可接受 |

#### 4.3.5 小练习与答案

**练习 1**:自旋上限 `STREAM_SPIN_WAIT_CAP` 定为 5 ms,为什么必须有这个上限?又为什么定在毫秒量级而不是更短或更长?

**答案**:自旋忙等烧一个 CPU 核,若无上限,流一旦长时间不空(例如又撞上 bulk 拷贝)就会白烧 CPU。上限的作用是「短等待拿低延迟,长等待退回省 CPU 的阻塞同步」。定在 5 ms 是权衡:解码场景正常等待是微秒级,5 ms 已是「情况不正常」的信号,继续自旋的边际收益归零;太短会让偶发的慢步骤频繁落入阻塞同步(白付唤醒延迟),太长则在真异常时长时间占核。这是一个可调的工程常数,不是数学推出来的最优值。

**练习 2**:有人认为「传递覆盖」是投机取巧,应该老老实实在 argmax 后再加一次显式同步。请评估这个提议。

**答案**:多余且有害。同一条流按序执行是 CUDA 的契约(2.3 节),D2H 完成蕴含 argmax 完成是必然而非巧合,再加一次同步不会更正确,只会让步骤循环多付一次等待——而且若加的是 `ctx.sync()` 这类粗粒度原语,还会把无关流也冻住。注释里「equivalent to the old full-stream sync for this path」说的正是:旧的重同步可以安全地被这一次等待替代。

**练习 3**(思考题):`memcpy_dtoh` 固定走 `ctx.stream`(基流)入队,而 `stream_spin_wait` 等的是 `active_cu_stream`——若某线程正处于 stream override(副流激活)中调用 `select_batch`,两者指向不同的流。请依据本讲引用过的注释推断:哪一层保证读到的仍是对的 token?

**答案**:依据 [src/lib.rs:71-79](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L71-L79) 的字段注释「the reader blocks only on the copy's own event」推断:`argmax_host.as_slice()` 这一层兜底——它等待的是落进这个 pinned 缓冲的那次拷贝本身完成,与 `stream_spin_wait` 等哪条流无关。此时 `stream_spin_wait` 等的副流可能提前排空而返回过早,但 `as_slice()` 仍会等到基流上那次 D2H 真正落地才放行。所以正确性由 pinned 读取语义兜住,`stream_spin_wait` 在 override 下退化为一次「可能无用的额外等待」。(此答案以代码注释为依据推断,cudarc `as_slice` 的内部实现本仓库未 vendored,未逐行核实——标注:待本地验证。)

## 5. 综合实践

写一篇**机制笔记**(建议 300-500 字加两张时序图,可直接记进你的学习笔记仓库),题目:「从 #704 看 D2H 回读的三条纪律」。要求完成三部分:

1. **事故时序图**:参照 4.1.2 节的格式,自己重画 #704——要素必须齐全:步骤线程、拷贝通道、KV 恢复洪流、pageable 回读的同步语义(线程原地等待)、23.6 ms 冻结所有活跃流的 token 交付。图下配一句话点出根因:「等待时长由通道上所有前车决定」。
2. **修复时序图**:pinned 方案——D2H 异步入队即返回,读取方只阻塞在拷贝自身事件;洪流与小回读并行调度,token 交付亚毫秒。图下配一句话点出代价边界:「等待时长只由这次拷贝自身决定」。
3. **传递覆盖论证**:引用 [src/lib.rs:251-254](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L251-L254) 的注释原文,分三步(入队顺序 → 同流按序执行 ⇒ 等价性)论证 `stream_spin_wait` 那一次等待为何等价于旧的全流同步,并补一句自旋的动机(把调度器唤醒从步骤循环里拿掉)。

**自查清单**(写完对照):

- 事故图里 pageable 的「同步拷贝语义」有没有画成线程等待?
- 修复图里有没有错误地把 pinned 画成「更快」而不是「等待边界更小」?(pinned 不加速拷贝,只隔离等待)
- 论证里有没有用到「同流按序执行」这个前提?(缺了它,传递覆盖不成立)
- 有没有把 write-combined 的告诫(不扩成热点读取结构、落地后才读)记进去?

本实践全程无需 GPU,也无需运行任何命令。

## 6. 本讲小结

- **#704 根因**:对 pageable 页的 D2H 带同步拷贝语义(驱动不信任可换页地址,只能等数据亲手落进用户页),等待时长继承了拷贝通道上所有前车的延迟——撞上 KV 恢复 bulk 洪流时,亚毫秒回读变 23.6 ms,冻结所有活跃流的 token 交付。
- **修复**:`SampleScratch` 的 `argmax_host`/`argmax_host_alt` 用 `unsafe alloc_pinned` 分配固定页,D2H 异步入队即返回,读取方只阻塞在拷贝自身事件上;等待边界从「通道」缩到「这次拷贝」。
- **write-combined 告诫**:该固定页 CPU 读不缓存——`max_rows` 个 i32 的一次顺序扫无所谓,但绝不能扩成热点循环读取的结构;且初始内容未定义,只能在 D2H 落地之后读。
- **stream_spin_wait**:自旋轮询 `cuStreamQuery` 最多 5 ms、超时退回阻塞同步——为微秒级的解码等待避开调度器唤醒开销。
- **传递覆盖**:同流按序执行,回读排在 H2D 与 argmax 之后,等回读完成即传递覆盖全部前序工作,等价于旧的全流同步,无需再加显式同步。
- **三档等待粒度各就各位**:解码步骤循环内用 `stream_spin_wait`(流级自旋)或 `as_slice`(单拷贝事件),按需路径(`token_logprobs_batch`)才用 `ctx.sync()`(全设备)。

## 7. 下一步学习建议

下一讲 **u4-l3「模型无关的测试策略与性能基准」** 收束整个手册:本讲的 #704 属于「历史教训冻结成注释与回归锚点」的传统——`pegainfer-kv-cache/tests/restore_commit.rs` 的文件头注释就是这种写法的范例。读完 u4-l3 后,建议回到 `pegainfer-kernels/src/tensor.rs` 通读一遍流与同步原语(`StreamOverrideGuard`、`active_cu_stream`、`stream_spin_wait`、`memcpy_dtod_u32_from_i32`),再顺着 `docs/models/` 目录里与 #704 同期的文档,看一次真实事故如何同时催生采样侧修复(pinned 落地区)、恢复侧修复(传输后端选择)与回归锚点(36 ms → 1.6 ms)三条战线——那是「教训如何进代码」的完整标本。
