# 异步屏障与异步拷贝

## 1. 本讲目标

本讲深入 Hopper/Ampere 级 GPU 的**异步**数据搬运与同步机制。GPU 上最慢的操作往往不是计算，而是「把数据从全局显存（HBM）搬进共享内存」。如果用普通 `*ptr` 读，线程会卡在那里等数据；而 `cp.async` 让线程**发起**搬运后立刻继续干别的活，硬件在后台把全局内存 DMA 进共享内存，线程稍后再来「问一句搬完了没」。

学完本讲，你应当能够：

1. 说清 **mbarrier 异步屏障**与 `sync_threads()` 阻塞屏障的本质区别，以及为什么「异步屏障」能跟踪硬件事务而后者不能。
2. 声明并用 `cp.async`（含 `zfill` 零填充变体）发起全局→共享的异步拷贝，并用 `commit_group` / `wait_group` / `wait_all` 或 mbarrier 等待完成。
3. 解释 **arrive / wait / token / phase** 协议：线程到达屏障拿到令牌、轮询等待同一个相位；以及 TMA 场景下的 `expect_tx` 事务字节计数与 `parity` 奇偶等待。
4. 把上述原语组合成「**搬运与计算重叠**」的软件流水线（double buffering），体会它与「同步拷贝再计算」在时序上的差异。

本讲承接 u5-l1（warp 级原语）与 u2-l3（共享内存与同步）：本讲仍是「编译器识别的桩函数」（`unreachable!()` 占位、由 mir-importer 译成方言 op、由 mir-lower 降级为 PTX），但关注点从「线程间通信」升级到「线程与硬件异步单元之间」的通信。

## 2. 前置知识

在进入异步机制前，先用三段话建立直觉。

**（1）普通加载是同步的、会阻塞线程。** 当线程执行 `let v = *ptr;` 而 `ptr` 指向全局内存时，这条指令要等数据真的从 HBM 到达寄存器才继续。搬运期间这个线程的计算资源（ALU）闲置。一个 block 里若有大量线程都在等全局内存，整块 SM 的吞吐就被内存延迟拖垮。

**（2）`cp.async` 把「发起」与「完成」拆开。** 线程调用 `cp.async` 只是「下单」：告诉硬件「请把这片全局内存搬到这片共享内存」，然后**立刻继续执行下一条指令**。搬运由硬件异步执行单元完成，不占用该线程。线程在**真正要用**这片共享内存之前，必须显式「等单」（wait），否则会读到未定义数据。这种「先下单、后取货」的解耦是重叠的基础——下单后到取货前，线程可以干别的计算。

**（3）怎么知道「搬完了」？** 有三类完成信号：

- **分组等待（group wait）**：用 `cp.async.commit_group` 把若干个已下单的 cp.async 打成一个组，再用 `cp.async.wait_group N` 等到「最多还剩 N 组未完成」或 `cp.async.wait_all` 等全部完成。组是**每线程独立**、按下单顺序 FIFO 的。
- **mbarrier + expect_tx**：让一个 mbarrier 屏障同时跟踪「线程到达数」与「异步搬运字节数」，两者都满足才算完成。这是 Hopper TMA 的主力同步方式，也最能体现「屏障跟踪硬件事务」。
- **`sync_threads`？不行。** `bar.sync 0` 只能挡住线程，不能跟踪异步搬运；它本身是阻塞式的、不感知 cp.async / TMA。

> 名词速查：CTA = Cooperative Thread Array = 一个 thread block；smem = shared memory；gmem = global memory；phase bit = 屏障的相位位，用于在循环里复用同一个屏障而不必反复 init。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`crates/cuda-device/src/barrier.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs) | mbarrier 异步屏障的全部设备端 API：`Barrier` 类型、`mbarrier_init/arrive/wait/try_wait/test_wait/arrive_expect_tx/...`，以及更安全的 typestate 封装 `ManagedBarrier` |
| [`crates/cuda-device/src/async_copy.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/async_copy.rs) | `cp.async` 异步拷贝 API：`cp_async_ca_4/8` 与 `cp_async_ca_zfill_4/8/16` |
| [`crates/rustc-codegen-cuda/examples/cp_async_small/src/main.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cp_async_small/src/main.rs) | 4/8 字节 `cp.async` 端到端示例：下单→`commit_group`→`wait_all`→`sync_threads`→读共享内存 |
| [`crates/rustc-codegen-cuda/examples/barrier/src/main.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/barrier/src/main.rs) | mbarrier 端到端示例：`init`→`sync_threads`→`arrive`→`test_wait` 自旋→`inval` |
| [`crates/rustc-codegen-cuda/examples/cp_async_zfill/src/main.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cp_async_zfill/src/main.rs) | zfill 零填充示例：`src_size < cp_size` 时硬件补零 |
| [`crates/mir-importer/src/translator/terminator/intrinsics/cp_async.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/cp_async.rs) | 编译器把 `cp_async_ca_*` 调用译成 `dialect-nvvm` 的 `CpAsyncCa*Op` |
| [`crates/mir-importer/src/translator/terminator/intrinsics/sync.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/sync.rs) | 编译器把 `sync_threads`、`mbarrier_*` 译成 `Barrier0Op`、`Mbarrier*Op` 等方言 op |

> 阅读提示：本讲引用的所有设备端函数体都是 `unreachable!("... called outside CUDA kernel context")` 的占位桩。它们在宿主上永远不该被调用；真正语义由 cuda-oxide 编译器在翻译内核时注入。这一点和 u2-l3、u5-l1 完全一致——「函数名」就是编译器的识别钩子。

## 4. 核心概念与源码讲解

### 4.1 mbarrier 异步屏障

#### 4.1.1 概念说明

`sync_threads()` 是一个**阻塞式、纯线程**的屏障：块内所有线程必须全部到达，所有人一起自旋，然后一起放行。它有两个根本局限——

1. 它**只认线程**，不认硬件异步事务。一个 `cp.async` 是硬件 DMA 单元干的活，它不会去「到达」`bar.sync`。所以你无法用 `sync_threads` 等「一块正在飞的共享内存搬运」完成。
2. 它**阻塞**：到达后线程就只能干等，不能借机做计算。

**mbarrier**（memory barrier，硬件屏障）是一个放在共享内存里的 64 位状态字，由 GPU 硬件直接维护，解决上述两点：

- 它可以接收**两类到达**：线程到达（`mbarrier.arrive`）和**事务到达**（`mbarrier.arrive.expect_tx`，由 cp.async.bulk / TMA 这类硬件 DMA 在搬完时自动贡献）。
- 屏障完成条件是「**期望的到达数都到了** 且 **期望的事务字节数都搬完了**」——两个条件取**逻辑与**。这正是它能跟踪异步搬运的关键。
- 线程到达后拿到一个 **token**（编码当前相位），可以**先去干别的**，之后再用 `try_wait` 轮询，实现「下单→干计算→回来取货」的非阻塞模式。

一句话：`sync_threads` 是「大家约好一起到齐再走」；mbarrier 是「到齐（含硬件事务）就置位，谁要用谁去查」。前者阻塞、只认线程；后者非阻塞、能认硬件事务。

#### 4.1.2 核心流程

一个 mbarrier 的标准生命周期是四步：

```
① 声明        static mut BAR: Barrier = Barrier::UNINIT;     // 共享内存里的 8 字节
② 初始化      线程0: mbarrier_init(&BAR, expected_count);     // 告诉硬件「期望到达数」
              fence_proxy_async_shared_cta();                 // (TMA 场景必需) 让异步代理看到 init
              sync_threads();                                 // 让全块看到已初始化的屏障
③ 使用        各线程: token = mbarrier_arrive(&BAR);          // 到达，拿相位 token
              ... 可以做不依赖该屏障保护的数据的计算 ...
              mbarrier_wait(&BAR, token);                     // 等本相位完成（内部自旋 try_wait）
④ 释放        线程0: mbarrier_inval(&BAR);                    // 释放硬件资源（可重新 init 复用）
```

屏障内部用一个 **phase bit（相位位）**：每完成一次，相位翻转。这样**同一个屏障可以在循环的每次迭代里复用**，不必每轮重新 `init`——线程拿到的是「当前相位」的 token，等到「该相位完成」即返回，下一轮自然切换到新相位。

#### 4.1.3 源码精读

`Barrier` 本身只是一个 8 字节、8 字节对齐的不透明包装，真实状态由硬件管理：

> [`crates/cuda-device/src/barrier.rs:87-92`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L87-L92) —— `Barrier` 是 `#[repr(C, align(8))]` 的 64 位值，对应 PTX 里的 `mbarrier` 对象（共享内存中、8 字节对齐）。`UNINIT` 常量用于 `static mut` 声明（[`barrier.rs:102`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L102)）。

初始化只应被**单个线程**调用一次（通常线程 0）：

> [`crates/cuda-device/src/barrier.rs:141-146`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L141-L146) —— `mbarrier_init(bar, expected_count)` 编译期识别为 `mbarrier.init.shared.b64 [addr], count;`。注释 `// Lowered to: call void @llvm.nvvm.mbarrier.init.shared(...)` 说明它最终落到一条 LLVM NVVM intrinsic。

可以看到典型的「编译器识别桩」三件套：`#[inline(never)]`（保证调用点不被内联掉，编译器才能在调用处识别）、`unsafe`、`unreachable!()` 函数体。函数名 `mbarrier_init` 即识别钩子。

完整的使用闭环在示例里一目了然：

> [`crates/rustc-codegen-cuda/examples/barrier/src/main.rs:41-58`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/barrier/src/main.rs#L41-L58) —— 线程 0 `mbarrier_init`，全块 `sync_threads()` 确保 init 可见，每个线程 `mbarrier_arrive` 拿 token，再用 `mbarrier_test_wait` **自旋**直到相位完成。

注意这里**仍需要 `sync_threads()`**——它的作用不是等 cp.async（这个例子里没有 cp.async），而是保证「线程 0 写入的 init 值」对全块所有线程可见。`mbarrier_init` 是普通线程的内存写，跨线程可见性仍靠 `bar.sync 0`。这一点在 4.3 节会再次强调。

对于想要更强编译期保障的代码，crate 还提供 typestate 封装 `ManagedBarrier`，用类型状态 `Uninit → Ready → Invalidated` 防止「未初始化就 arrive」或「重复初始化」：

> [`crates/cuda-device/src/barrier.rs:800-804`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L800-L804) —— `ManagedBarrier<State, Kind, ID>` 用 `PhantomData` 携带生命周期状态与用途标记。`init_by`（[`barrier.rs:909-925`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L909-L925)）封装了「仅 init_thread 执行 `mbarrier_init` + `fence_proxy_async_shared_cta`，全块 `sync_threads`」的标准模式，返回一个 `Ready` 句柄。

#### 4.1.4 代码实践

**实践目标**：跑通 mbarrier 的最小示例，确认它能像 `sync_threads` 一样同步全块。

1. 阅读 [`examples/barrier/src/main.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/barrier/src/main.rs) 的 `barrier_sync_test`。
2. 运行 `cargo oxide run barrier`（该示例从 `barrier.ptx` 加载模块，按其 README 确认编译产物路径）。
3. 把 `mbarrier_test_wait` 自旋循环的 `!` 取反（即改成 `while mbarrier_test_wait(...)`），重新编译运行。

**预期现象**：取反后条件恒为「未完成时退出循环」，线程会在屏障完成前就往下走、读到未同步的数据，`barrier_shared_data_test` 的「邻居值」校验大概率失败。

**待本地验证**：具体失败数值取决于调度，但「校验失败」是可预期的方向性结果。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mbarrier_init` 必须只由一个线程调用，而 `mbarrier_arrive` 要由所有参与线程调用？

> **答**：init 写入的是「期望到达数」这个配置，多线程重复写会破坏状态；而 arrive 是「我到了」的计数，期望到达数决定了多少个 arrive 才算齐，缺一个就永远卡住。

**练习 2**：`sync_threads()` 和 mbarrier 各自的「完成条件」是什么？

> **答**：`sync_threads`（`bar.sync 0`）—— 块内全部线程到达；mbarrier —— 全部期望线程到达 **且** 全部期望事务字节完成（若用了 `expect_tx`）。

---

### 4.2 arrive/wait 语义：token、phase 与 expect_tx

#### 4.2.1 概念说明

mbarrier 的 arrive/wait 协议围绕三个概念展开：

- **token（令牌）**：`mbarrier_arrive` 返回一个 64 位 token，编码「当前相位」。它不是随机数，而是让你**等对这个相位**——屏障会在相位翻转时完成，你拿着旧相位的 token 等到「旧相位完成」。
- **phase（相位）**：屏障内部的 1 位状态，每次完成后翻转。这让一个屏障能在 `for` 循环里反复用：第 i 次迭代 arrive 拿到相位 p，wait(p) 等到第 i 次完成；第 i+1 次自然进入相位 1-p。
- **expect_tx（事务字节）**：普通 arrive 只贡献「到达数」；`mbarrier_arrive_expect_tx(bar, tx_count, bytes)` 额外声明「还有 `bytes` 字节的异步搬运会到达本屏障」。屏障完成条件变成「到达数齐 ∧ 字节数齐」。这是给 cp.async.bulk / TMA 用的——TMA 搬完一 tile 会自动给屏障贡献字节数。

此外还有两种**等待**风格：

- **`test_wait`**：纯非阻塞探测，返回 `bool`。忙等（busy spin）用它会占满总线。
- **`try_wait`**：也返回 `bool`，但给硬件「调度提示」——硬件可以短暂挂起该线程，减少总线争用。源码注释明确写：「**这是 TMA 同步的首选等待操作**，nvcc 在 TMA 拷贝模式里用的就是它」。

#### 4.2.2 核心流程

以「线程发起搬运 + 线程到达」的最常见模式为例：

```
# 线程 0（生产者）：发起异步搬运，并告诉屏障要等多少字节
tma::copy_async(...);                                  # 或 cp.async（按字节累加需自行计数）
token = mbarrier_arrive_expect_tx(&BAR, 1, tile_bytes); # 声明「再等 tile_bytes 字节」

# 其他线程（消费者）：仅到达
token = mbarrier_arrive(&BAR);

# 所有人：非阻塞等待
while !mbarrier_try_wait(&BAR, token) {
    # 可穿插与该屏障无关的计算；空转时硬件可短暂挂起本线程
}
# 到这里：到达齐 ∧ 字节齐 → 搬运已落地，可安全读共享内存
```

屏障完成条件可写作：

\[
\text{complete} \iff \big(\text{arrivals} \ge \text{expected\_count}\big) \;\wedge\; \big(\text{tx\_bytes} \ge \text{expected\_bytes}\big)
\]

> 注意：当**不**使用 `expect_tx`（即纯线程屏障）时，`expected_bytes = 0` 恒成立，退化为「到达数齐」即可，与 4.1 的简单模式一致。

对于「生产者不返回 token」的场景（如 Blackwell 的 `tcgen05.commit` 直接给屏障贡献到达，但拿不到 token），改用 **parity（奇偶）等待**：不再传 token，而是传期望相位的奇偶（0/1），等屏障翻到该相位。

#### 4.2.3 源码精读

`mbarrier_arrive` 返回 token，`mbarrier_wait` 是对 `try_wait` 的自旋包装：

> [`crates/cuda-device/src/barrier.rs:183-188`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L183-L188) —— `mbarrier_arrive` 译为 `mbarrier.arrive.shared.b64 token, [addr];`，返回 64 位相位 token。
>
> [`crates/cuda-device/src/barrier.rs:357-365`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L357-L365) —— `mbarrier_wait` 是 `#[inline(always)]` 的纯 Rust 函数，循环调用 `mbarrier_try_wait`。注释点明选用 `try_wait` 而非 `test_wait` 是因为前者有调度提示、自旋更高效。

两种等待原语对照：

> [`crates/cuda-device/src/barrier.rs:240-245`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L240-L245) —— `mbarrier_test_wait` 译为 `mbarrier.test_wait.shared.b64 pred, [addr], token;`，纯非阻塞探测。
>
> [`crates/cuda-device/src/barrier.rs:285-290`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L285-L290) —— `mbarrier_try_wait` 译为 `mbarrier.try_wait.shared.b64 pred, [addr], token;`，**TMA 同步首选**。

事务字节到达与奇偶等待：

> [`crates/cuda-device/src/barrier.rs:414-420`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L414-L420) —— `mbarrier_arrive_expect_tx(bar, tx_count, bytes)` 译为 `mbarrier.arrive.expect_tx.shared.b64 token, [addr], bytes;`，屏障在到达数与字节数都满足后才完成。
>
> [`crates/cuda-device/src/barrier.rs:307-311`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L307-L311) —— `mbarrier_try_wait_parity(bar, parity)` 译为 `mbarrier.try_wait.parity.shared::cta.b64 pred, [addr], parity;`，给 `tcgen05.commit` 这类无 token 生产者用。

`ManagedBarrier` 把这套协议包成类型安全的 `arrive() / arrive_expect_tx(bytes) / wait(token) / try_wait(token)`，token 也升级成 newtype `BarrierToken` 防止误传裸 `u64`：

> [`crates/cuda-device/src/barrier.rs:961-975`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L961-L975) —— `arrive_expect_tx(&self, bytes)` 与 `wait(&self, token)`，token 是 `BarrierToken` 而非裸 `u64`。

#### 4.2.4 代码实践

**实践目标**：体会 token/phase 的「下单→干计算→取货」非阻塞能力。

1. 阅读 [`examples/barrier/src/main.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/barrier/src/main.rs) 的 `barrier_sync_test`，把自旋 `mbarrier_test_wait` 换成 `mbarrier_wait`（等价，但更简洁）。
2. 在 `arrive` 与 `wait` 之间插入一段「不依赖共享内存」的纯计算（例如循环累加一个寄存器若干次），观察这相当于「免费」塞进了等待时间里。

**预期现象**：结果数值不变（这段插入计算不影响屏障语义）；如果用 nvprof/Nsight Compute 测量，理论上插入计算后的内核总时间不增反可能略降，因为自旋被打断。

**待本地验证**：性能差异需用 Nsight Compute 在真实 Hopper/Blackwell 上测，本机无 GPU 时仅作代码阅读型实践。

#### 4.2.5 小练习与答案

**练习 1**：`test_wait` 和 `try_wait` 都返回 `bool` 且都不阻塞，为什么自旋等待要优先用 `try_wait`？

> **答**：`try_wait` 给硬件调度提示，硬件可短暂挂起自旋线程、降低共享总线争用；`test_wait` 是纯探测，密集自旋会与其他线程的内存访问抢总线。

**练习 2**：什么场景下必须用 `try_wait_parity` 而不能用 `try_wait(token)`？

> **答**：当生产者通过不返回 token 的方式到达屏障（如 `tcgen05.commit` 直接贡献到达），消费者拿不到 token，只能按相位的奇偶等。

---

### 4.3 cp.async 异步拷贝与 zfill

#### 4.3.1 概念说明

`cp.async` 是 Ampere（sm_80）引入的「线程发起、硬件执行」的全局→共享异步拷贝。和普通 `ld.global` + `st.shared` 的两步软件搬运相比，它的关键优势是：**线程只下单、不等结果**，搬运走硬件直通路径，省掉中间寄存器转手。

本 crate 提供两类变体（见 [`async_copy.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/async_copy.rs) 顶部表格）：

- **不带零填充**：`cp_async_ca_4` / `cp_async_ca_8` —— 整字搬运，PTX `cp.async.ca.shared.global [smem], [gmem], N;`。
- **带零填充（zfill）**：`cp_async_ca_zfill_4/8/16` —— 多一个 `src_size` 参数，只拷 `src_size` 字节，剩余 `cp_size - src_size` 字节由**硬件补零**。

**zfill 解决什么问题？** 分块（tiled）算法里，最后一块往往不是整块——比如 tile 大小 16 字节但只剩 3 字节有效数据。若用普通拷贝，要么越界读、要么加分支。zfill 让你**始终按整块拷**，无效部分硬件填 0。由于 0 在加法/乘法/多数归约里是单位元，下游计算可以**无分支**地处理整块。

> 关于缓存策略：所有变体都是 `.ca`（cache-all-levels）。`.cg`（cache-global）策略只支持 16 字节拷贝，故本 crate 仅提供 `.ca` 变体（见文件头注释）。

#### 4.3.2 核心流程

一个完整的 cp.async「下单→等单→用」三段式：

```
# ① 下单（每线程各自下单自己的元素）
cp_async_ca_4(smem_ptr, gmem_ptr);                     # 异步，立即返回

# ② 等单：三选一
#   (a) 粗暴：等所有在飞拷贝
ptx_asm!("cp.async.commit_group;", clobber("memory"));
ptx_asm!("cp.async.wait_all;",   clobber("memory"));

#   (b) 组等待：等至只剩 N 组未完成（更细粒度，适合流水线）
#      ptx_asm!("cp.async.commit_group;",  clobber("memory"));
#      ptx_asm!("cp.async.wait_group N;",  clobber("memory"));

#   (c) mbarrier 跟踪：见 4.4 节

# ③ 跨线程同步后才能读别人写到的共享内存
sync_threads();
let v = smem[tid];
```

两个**必须记住的坑**：

1. **`commit_group` / `wait_*` 是每线程的**。线程 A 下的单，只有线程 A 自己 wait 才等得到。如果线程 B 要读线程 A 搬来的共享内存，A wait 完之后还必须 `sync_threads()` 让 B 也看到数据。
2. **手写的完成类 PTX 必须带 `clobber("memory")`**。否则编译器可能把「wait 之后读共享内存」重排到「wait 之前」，读到旧值。`ptx_asm!` 的 `clobber("memory")` 就是告诉编译器「这条内联汇编会改内存，别跨越它移动访存」。

#### 4.3.3 源码精读

`cp_async_ca_4` 是最典型的「识别桩」：

> [`crates/cuda-device/src/async_copy.rs:68-72`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/async_copy.rs#L68-L72) —— `pub unsafe fn cp_async_ca_4(_shared_dst: *mut u32, _global_src: *const u32)`，函数体只有 `unreachable!(...)`。函数名 `cp_async_ca_4` 即编译器识别钩子，`#[inline(never)]` 保证调用点存活。

zfill 变体多一个 `src_size`：

> [`crates/cuda-device/src/async_copy.rs:152-155`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/async_copy.rs#L152-L155) —— `cp_async_ca_zfill_4(_shared_dst, _global_src, _src_size: u32)`，译为 `cp.async.ca.shared.global [...], [...], 4, src_size;`。其 safety 文档（[`async_copy.rs:115-151`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/async_copy.rs#L115-L151)）逐条列出了对齐、生命周期、完成方式等约束。

编译器如何把这些桩译成 PTX？mir-importer 在 `terminator/mod.rs` 按函数名分派到 `emit_cp_async_ca_4`，后者构造一个 `dialect-nvvm` 的 `CpAsyncCa4Op`：

> [`crates/mir-importer/src/translator/terminator/intrinsics/cp_async.rs:131-155`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/cp_async.rs#L131-L155) —— `emit_cp_async_ca_4` 翻译 2 个参数后，用 `Operation::new(ctx, CpAsyncCa4Op::get_concrete_op_info(), ...)` 生成方言 op（[`cp_async.rs:120`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/cp_async.rs#L120)），分派入口在 [`terminator/mod.rs:4005`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/mod.rs#L4005)。
>
> [`crates/dialect-nvvm/src/ops/cp_async.rs:44-50`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/cp_async.rs#L44-L50) —— `CpAsyncCa4Op` 的方言名是 `nvvm.cp_async_ca_4`，声明 2 个操作数、0 个结果，对应 `cp.async.ca.shared.global [%smem32], [$1], 4;`。这条 op 之后由 mir-lower 降级为最终的 PTX 指令。

端到端示例的「下单→等单」紧凑三行：

> [`crates/rustc-codegen-cuda/examples/cp_async_small/src/main.rs:43-50`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cp_async_small/src/main.rs#L43-L50) —— `cp_async_ca_4` 后紧跟 `ptx_asm!("cp.async.commit_group;", clobber("memory"))` 与 `ptx_asm!("cp.async.wait_all;", clobber("memory"))`，再用 `sync_threads()` 让全块可见。

宿主侧的硬件门槛守护：

> [`crates/rustc-codegen-cuda/examples/cp_async_small/src/main.rs:107-114`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cp_async_small/src/main.rs#L107-L114) —— `cp.async` 要求 `sm_80+`，宿主在读到 `compute_capability` 后若 `major < 8` 直接跳过并判 PASS。这正是 u1-l5 提到的「能编译 ≠ 能运行」。

#### 4.3.4 代码实践

**实践目标**：跑通 4 字节异步拷贝，并用 zfill 验证硬件补零。

1. 运行 `cargo oxide run cp_async_small`，确认 4/8 字节拷贝的 32/64 个元素逐一正确。
2. 运行 `cargo oxide run cp_async_zfill`，关注 Test 2（`src_size=2`）：输入 `0xAABBCCDD`，小端序下拷低 2 字节 `0xCCDD`、高 2 字节补零，应得 `0x0000CCDD`；Test 3（`src_size=0`）应整字为 `0`。
3. 若本机无 sm_80+ GPU，上述示例会打印 `PASS (skipped)`——此时改为阅读 [`cp_async_zfill/src/main.rs:124-154`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cp_async_zfill/src/main.rs#L124-L154) 的断言，理解为什么期望值是 `0x0000_CCDD`。

**预期结果**：在有 sm_80+ GPU 的机器上，三个测试全部 PASS；在更老的 GPU 上 skipped。

#### 4.3.5 小练习与答案

**练习 1**：`cp_async_ca_zfill_16` 的 `src_size` 取值范围是多少？为什么？

> **答**：`0..=16`（含端点）。源码注释说明 CUDA 自带 pipeline/CCCL 助手对「整块拷贝」用 `src_size == cp_size`，故合法范围是 `0..=cp_size`；超过 `cp_size` 非法。

**练习 2**：为什么 `commit_group` / `wait_all` 的内联 PTX 必须带 `clobber("memory")`？

> **答**：完成类指令改变了共享内存的可见内容。没有内存 clobber，编译器会把 wait 之后的读重排到 wait 之前，读到未完成的旧数据。clobber 告诉编译器「别跨越这条汇编移动访存」。

---

### 4.4 计算-搬运重叠流水线

#### 4.4.1 概念说明

把 4.1–4.3 串起来，就得到 GPU 上最重要的优化范式之一：**软件流水线 / double buffering**。

朴素模式（同步拷贝）的时间线是**串行**的：

```
tile0: [拷贝][----计算----][拷贝][----计算----] ...
```

总时间约为

\[
T_{\text{seq}} = \sum_i \big(T_{\text{copy},i} + T_{\text{compute},i}\big)
\]

搬运时计算单元闲置、计算时搬运单元闲置。

重叠模式利用 cp.async 的「下单即走」：在计算 tile N 的同时，提前下单搬运 tile N+1。理想情况下时间线变成：

```
拷贝:  [ tile0 ][ tile1 ][ tile2 ] ...
计算:           [ tile0 ][ tile1 ][ tile2 ] ...
```

稳态下每步耗时取两者较大值：

\[
T_{\text{overlap}} \approx \sum_i \max\big(T_{\text{copy},i},\; T_{\text{compute},i}\big)
\]

当 \(T_{\text{copy}} \approx T_{\text{compute}}\) 时，吞吐近乎翻倍；当一方远大于另一方，瓶颈侧决定上限，但仍优于串行。

实现重叠需要三件事配合：

1. **双缓冲**：准备两份共享内存 tile（A/B）。线程在算 A 时，下单搬 B；下一轮交换。
2. **mbarrier 跟踪每 tile 的搬运**：用 `arrive_expect_tx`（TMA）或组等待（cp.async）精确知道「tile N 搬完了」。
3. **相位复用**：每个 tile 一个屏障，按 phase 在循环里复用，避免每轮 `init`。

> 重要澄清：[`cp_async_small`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cp_async_small/src/main.rs) 示例用的是 `wait_all`——它是**等全部在飞拷贝完成**，属于「等死」模式，**不产生重叠**。它只演示「cp.async 能用」，不演示「cp.async 能重叠」。真正展示重叠需要把它改成组等待或 mbarrier，这正是本节的实践与综合实践要做的。

#### 4.4.2 核心流程

一个最小双缓冲循环的伪代码（cp.async + mbarrier 版本）：

```
# 预热：先下单第 0 块
cp.async tile0 → smemA;  commit_group;          # group 0
mbarrier_arrive_expect_tx(BAR_A, 1, tile_bytes); # 让 BAR_A 等 tile0 字节

for i in 0..N {
    # (a) 下单下一块到另一个 buffer
    next = if i % 2 == 0 { smemB } else { smemA };
    cp.async tile(i+1) → next;  commit_group;
    mbarrier_arrive_expect_tx(BAR_next, 1, tile_bytes);

    # (b) 等当前块搬完（这块搬运与本循环的计算重叠）
    cur  = if i % 2 == 0 { smemA } else { smemB };
    mbarrier_wait(BAR_cur, token_cur);

    # (c) 计算当前块——此时下一块的搬运正在飞
    compute(cur);
}
```

关键在于 (b) 的 `wait` 之前，已经下了 (a) 的下一块单；wait 期间及 compute 期间，硬件在搬下一块。这就是「搬运与计算重叠」。要严格实现这个模式，需要用 `ManagedBarrier` 的 `TmaBarrier0` / `TmaBarrier1` 类型别名（[`barrier.rs:1083-1086`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L1083-L1086)）区分两个 buffer 的屏障。

#### 4.4.3 源码精读

typestate API 让双缓冲的「两个屏障」在类型层不混：

> [`crates/cuda-device/src/barrier.rs:1083-1086`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L1083-L1086) —— `TmaBarrier0<S>` 与 `TmaBarrier1<S>` 是 `ManagedBarrier<S, TmaBarrier, 0/1>`，靠 const generic `ID` 区分两个缓冲各自的屏障，编译期就不会把 buffer A 的 token 喂给 buffer B 的 wait。

异步代理栅栏是 TMA 流水线里最隐蔽、也最致命的一环：

> [`crates/cuda-device/src/barrier.rs:573-577`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L573-L577) —— `fence_proxy_async_shared_cta()` 译为 `fence.proxy.async.shared::cta;`。NVIDIA GPU 有两套内存「代理」：普通线程操作走 generic proxy，TMA/cp.async 这类硬件异步操作走 async proxy。`mbarrier_init` 是 generic proxy 的写，**不主动 fence 的话 async proxy 可能看不到这次 init**，导致 TMA 给一个「硬件认为还没初始化」的屏障下单、行为未定义。因此 init 之后、发起 TMA 之前**必须**调它。注释明确：「Critical for TMA」。

`ManagedBarrier::init_by` 已经把这个 fence 内置进了初始化流程：

> [`crates/cuda-device/src/barrier.rs:909-918`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L909-L918) —— init 线程在 `mbarrier_init` 之后**立即** `fence_proxy_async_shared_cta()`，再 `sync_threads()`。用 typestate API 就不会漏掉这道 fence。

`mbarrier_arrive_and_wait` 是「到达即等」的便捷封装，适合不需要塞计算、只想当普通屏障用的场合：

> [`crates/cuda-device/src/barrier.rs:680-684`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L680-L684) —— `mbarrier_arrive_and_wait` 就是 `arrive` + `wait` 两步合一。

#### 4.4.4 代码实践

**实践目标**：把 cp.async 的「等死」改成「能重叠」的最小改动，体会 wait 粒度的差别。

1. 打开 [`examples/cp_async_small/src/main.rs`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cp_async_small/src/main.rs)，定位 [`main.rs:43-47`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cp_async_small/src/main.rs#L43-L47) 的 `wait_all`。
2. 阅读思考：本例只有「下单一块→等全部→算」，没有「下一块的单」，所以无论如何改 wait 都不会重叠。要重叠必须先有「多块」。
3. 进阶（综合实践见第 5 节）：把它扩展成两块缓冲 + 两次下单 + `wait_group`，使第二次下单发生在第一次 wait 之后、第二次 wait 之前——这才是重叠的种子。

**预期现象**：仅替换 `wait_all` 为 `wait_group 0`（语义等价于等全部）不会改变时序；只有引入「提前下单下一块」才会产生重叠。理解这一点比真的跑出加速更重要。

**待本地验证**：重叠带来的加速需在真实 GPU 上用 Nsight Compute 测量内存利用率。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `wait_all` 模式不可能产生搬运-计算重叠？

> **答**：`wait_all` 等所有在飞拷贝完成才继续，此时线程无事可做、纯等；且没有「在计算时让下一块在飞」的下单顺序，搬运与计算在时间上不重叠。

**练习 2**：双缓冲里，为什么对 buffer A 和 buffer B 用**两个不同的屏障**（`TmaBarrier0` / `TmaBarrier1`），而不是一个屏障？

> **答**：一个屏障的相位按到达顺序翻转，混用会让「等 buffer A 搬完」与「等 buffer B 搬完」相互干扰、相位错乱。两个独立屏障各自跟踪各自 buffer 的事务字节，token 不会串。

---

## 5. 综合实践

设计一个贯穿本讲的小任务：**把 `cp_async_small` 改造成一个最小双缓冲流水线内核**，把 4.1–4.4 全部用上。

**任务描述**：写一个 `#[kernel]`，把长度为 `N = BLOCK * STAGES` 的 `input: &[u32]` 搬到共享内存再原地「乘 2」写回 `out`。要求：

- 用两个共享内存 buffer（`SMEM_A`、`SMEM_B`），每个 `BLOCK` 个元素。
- 用两个 `Barrier`（或 `ManagedBarrier<_, TmaBarrier, 0/1>`），分别跟踪两个 buffer 的搬运完成。
- 主循环里：先 `cp.async` 下单下一块，再 `mbarrier_wait` 等当前块，再计算当前块——让「下一块的搬运」与「当前块的计算」在时间上重叠。
- 完成等待用 cp.async 组等待（`cp.async.commit_group` + `cp.async.wait_group 0`）或 mbarrier 的 `arrive_expect_tx` 任选其一。

**推荐步骤**：

1. 复制 `cp_async_small` 示例为新 crate（参考 u1-l3 的 `cargo oxide new`）。
2. 声明 `static mut SMEM_A: SharedArray<u32, BLOCK> = SharedArray::UNINIT;` 与 `SMEM_B`，以及两个 `static mut BAR_A/BAR_B: Barrier = Barrier::UNINIT;`。
3. 线程 0 `mbarrier_init(&BAR_A, block_size)` + `fence_proxy_async_shared_cta()`，全块 `sync_threads()`。
4. 预热：下单第 0 块到 `SMEM_A`。
5. 循环每个 tile：下单下一块到另一个 buffer → `mbarrier_arrive` + 自旋 `mbarrier_try_wait` 等当前块 → 计算当前块 → 写回 `out`。
6. 宿主侧 `unsafe { module.your_kernel(&stream, cfg, &input_dev, &mut out_dev) }`，核对每个元素是否为输入的两倍。

**关键自检点**：

- 你是否在每个 `cp.async` 后用了 `commit_group`？是否在 wait 类内联 PTX 上带了 `clobber("memory")`？
- 你是否在「线程 A 搬、线程 B 读」之间加了 `sync_threads()`？
- 用 mbarrier 时，初始化后是否调了 `fence_proxy_async_shared_cta()`（若用 cp.async 而非 TMA，理论上 cp.async 的 completion 不强制需要 async proxy fence，但加上无害；若改成 TMA 则必加）？

**待本地验证**：本综合实践需要 sm_80+ GPU 才能运行；无 GPU 时作为源码阅读与设计型实践完成，重点在「画出两个 buffer 的时间线，标出哪段搬运与哪段计算重叠」。

## 6. 本讲小结

- **mbarrier 是异步、可跟踪硬件事务的屏障**；`sync_threads` 是阻塞、只认线程的屏障。需要等「在飞的内存搬运」完成时只能用前者。
- **arrive/wait/token/phase 协议**：到达拿相位 token，非阻塞轮询 `try_wait`（首选）或 `test_wait` 等到该相位完成；相位位让一个屏障能在循环里复用。`expect_tx` 让屏障额外等「事务字节数」，是 TMA 同步的核心；无 token 生产者用 `parity` 奇偶等待。
- **cp.async 把搬运的「下单」与「完成」解耦**：线程下单即走，硬件后台 DMA；必须用 `commit_group`/`wait_group`/`wait_all` 或 mbarrier 显式等单，且等单类内联 PTX 必须带 `clobber("memory")` 防重排。
- **zfill** 让边界 tile 始终按整块拷、无效部分硬件补零，下游可无分支处理，`src_size ∈ 0..=cp_size`。
- **计算-搬运重叠**靠双缓冲 + 精粒度 wait（组等待或 mbarrier）+ 相位复用实现；`wait_all` 是等死模式，不产生重叠。
- 本讲所有设备端 API 仍是「编译器识别的桩」（`#[inline(never)]` + `unreachable!()`），由 mir-importer 译成 `dialect-nvvm` 的 `CpAsyncCa*Op` / `Mbarrier*Op` / `Barrier0Op`，再由 mir-lower 降级为 PTX。`fence_proxy_async_shared_cta` 在 generic proxy 与 async proxy 之间搭桥，是 TMA 同步不可漏的一环。

## 7. 下一步学习建议

- **TMA（张量内存加速器）**：本讲的 `arrive_expect_tx` + `parity` 是为 TMA 准备的。下一讲 u5-l5 会讲 `cp.async.bulk.tensor`（基于张量描述符的批量异步拷贝与多播），那时 mbarrier 才真正大显身手——建议紧接着学。
- **集群与分布式共享内存**（u5-l4）：`mbarrier_arrive_cluster`（[`barrier.rs:489-493`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L489-L493)）展示了 mbarrier 在集群 scope 下的跨 CTA 同步，与本讲的块内 scope 形成对照。
- **真实流水线范例**：仓库里 [`examples/mcast_barrier_test`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/mcast_barrier_test/src/main.rs) 与 [`examples/gemm_sol`](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/gemm_sol/src/main.rs) 把 cp.async/TMA + mbarrier + 双缓冲完整用在 GEMM 里，是本讲综合实践的最佳参照。
- **编译器侧深潜**：若想了解 `CpAsyncCa4Op`、`MbarrierInitSharedOp` 等方言 op 如何被 mir-lower 最终降级成 PTX 指令或 NVVM intrinsic，参看 u6-l3（mir-lower ops/intrinsics 深潜）。
