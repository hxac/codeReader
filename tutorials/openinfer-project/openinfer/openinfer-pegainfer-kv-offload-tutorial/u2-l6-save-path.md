# save 链路：fire-and-forget、排序契约与 keep_alive 契约

## 1. 本讲目标

读完本讲，你应该能够：

1. 说出 `build_saves` 如何把一组 `(block_id, hash)` 扇出到所有 layer，以及为什么 `i32 → usize` 的转换恰好发生在这一个边界上。
2. 逐行讲清 `submit_save` 的骨架：`spawn` + `oneshot` + `keep_alive` 的 drop 时机，以及空输入快速路径。
3. 用自己的话复述两条必须由**调用方**维护的契约：
   - **ORDERING CONTRACT**：为什么只能在 forward 步同步之后保存已封块，违反它读到的是撕裂数据；
   - **REUSE CONTRACT**：`keep_alive` 如何封死「请求结束 → slot 重分配 → 在途 D2H 抓到错 KV 写进旧 hash」的静默腐蚀窗口。
4. 区分 `save` / `save_blocking` / `submit_save` 三种形态在可见性与错误处理上的差异，理解 fire-and-forget 的日志兜底是怎么发生的。

## 2. 前置知识

本讲默认你已读完 u2-l4（引擎构造与注册）和 u2-l5（可轮询句柄）。在此基础上补充三个通用概念：

- **异步拷贝与 CUDA 流**。GPU 上的内存拷贝可以提交给一条「流」（stream）异步执行——调用立即返回，数据在后台搬运。pegaflow 的 D2H（Device-to-Host，显存到内存）拷贝运行在**它自己的流**上，与 pegainfer 写 KV 的计算流之间**没有任何依赖关系**。这是两条契约存在的物理根源：两条流各跑各的，谁也不等谁。
- **oneshot 通道**。`tokio::sync::oneshot` 是一条「单发单收」的通道：后台任务拿到 sender（`tx`），调用方拿到 receiver（`rx`）。后台任务 `tx.send(result)` 之后，receiver 侧才能收到结果；如果 receiver 先被 drop，`send` 会返回 `Err(未送达的值)`——engine.rs 正是用这个返回值实现日志兜底。
- **RAII guard（守卫）**。Rust 惯用法：一个类型在 `Drop` 时执行清理动作。pegainfer 的 kvbm 分配器提供 `KvBlockGuard` 这类 guard——一个块被 guard 持有期间不可被回收重用，guard drop 时块才归还池子。「持有一个 guard」=「钉住一个块」。

一个贯穿本讲的核心判断（承接 u1-l2 的时序红线）：**丢一次 save 只损命中机会（退化为重算），但把撕裂数据或错 KV 以正确-looking 的 hash 写进 host 池，才是致命的**——错的字节会顺着内容寻址被未来某个请求当作命中拉回来。两条契约防的正是后一种情况。

## 3. 本讲源码地图

| 文件 | 本讲涉及部分 | 作用 |
|------|------------|------|
| `pegainfer-kv-offload/src/engine.rs` | `build_saves` / `submit_save` / `save` / `save_blocking`（L205–L306） | save 链路的全部本体 |
| `pegainfer-kv-offload/src/handle.rs` | `SaveHandle` 类型别名与模块文档 | save 的收账句柄；「drop 句柄 ≠ 取消 I/O」的语义出处 |
| `pegainfer-qwen3/src/executor.rs` | `save_sealed_blocks`（L1836–L1877） | 真实调用方：展示两条契约在生产代码里如何被满足 |
| `pegainfer-kv-offload/tests/cpu_roundtrip.rs` | L110 | `save_blocking` 的同步调用方（集成测试） |

## 4. 核心概念与源码讲解

### 4.1 `build_saves`：跨层扇出与 i32→usize 转换

#### 4.1.1 概念说明

u2-l2 讲过：注册时每个模型层（layer）在 pegaflow 里是一个独立的 strided arena，用自己的 `layer_name` 寻址。所以「把块 7 保存到 CPU」并不是一次拷贝，而是 **L 次**拷贝——第 0 层的块 7、第 1 层的块 7……各自是显存里不同位置的一段字节。`build_saves` 就是把 pegainfer 视角的一个「块列表」翻译成 pegaflow 视角的「每层一份的保存清单」。

另一个小但重要的职责：**块号类型翻译**。pegainfer 沿用 kvbm/CUDA 惯例用 `i32` 携带块号，pegaflow 用 `usize` 索引 GPU 块。这个转换只在该边界做一次，之后 crate 内部（包括 `load` 里的 `dst_block_ids`，见 [engine.rs:L404-L405](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L404-L405)）统一走 pegaflow 的 `usize` 约定。

#### 4.1.2 核心流程

```
输入: block_ids: &[i32], block_hashes: &[Vec<u8>]   // 第 i 个 hash 对应第 i 个块
  │
  ├─ block_ids 全部 i32 → usize（一次性边界转换）
  │
  └─ 对 layer_names 中的每一层 name：
       产出 LayerSave {
           layer_name:  name 的克隆,
           block_ids:   同一份 usize 块号列表的克隆,
           block_hashes: 同一份 hash 列表的克隆,
       }
输出: Vec<LayerSave>，长度 = 层数
```

关键点：**块号和 hash 跨层不变，变的只有设备数据本身**——每层 arena 的基址不同，pegaflow 拿着 `(layer_name, block_id)` 就能按「基址 + 块号 × 步长」还原出该层数据的地址（u2-l4 讲过这套「烤进注册表」的寻址方式）。

#### 4.1.3 源码精读

[engine.rs:L205-L220](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L205-L220) —— `build_saves` 全文：

```rust
/// Fan one (block_id, hash) list across every layer — the device data
/// differs per layer, the ids and hashes don't.
fn build_saves(&self, block_ids: &[i32], block_hashes: &[Vec<u8>]) -> Vec<LayerSave> {
    // pegaflow indexes GPU blocks by `usize`; pegainfer carries them as
    // `i32` (its kvbm/CUDA convention). Convert once at this boundary —
    // block ids are slot indices, always non-negative.
    let block_ids: Vec<usize> = block_ids.iter().map(|&id| id as usize).collect();
    self.layer_names
        .iter()
        .map(|name| LayerSave {
            layer_name: name.clone(),
            block_ids: block_ids.clone(),
            block_hashes: block_hashes.to_vec(),
        })
        .collect()
}
```

- `self.layer_names` 来自构造时的 `reg.layer_names`（[engine.rs:L197](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L197)），即 u2-l2 里每层一个 arena 的注册名，顺序终身不变。
- `LayerSave` 是 `pegaflow_core` 的类型（见 [engine.rs:L20](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L20) 的 import），本 crate 不自定义保存描述——保持「翻译层」的自我定位。
- `id as usize` 是裸转换，安全性由注释里的领域事实背书：块号是 slot 索引，恒非负。

#### 4.1.4 代码实践

源码阅读 + 手算型实践（无需 GPU）：

1. **实践目标**：建立「一次 save = 层数 × 块数」的规模直觉，并确认扇出的克隆语义。
2. **操作步骤**：
   - 打开 [engine.rs:L207-L220](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L207-L220)，假设一个 36 层的模型，executor 一次封了 5 个块后调用 save。
   - 手算：产出多少个 `LayerSave`？所有 `LayerSave` 加起来一共持有多少个块 id 条目、多少份 hash 克隆？`block_ids.clone()` 执行了几次？
   - 再读 [engine.rs:L252](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L252) 的 `debug_assert_eq!(block_ids.len(), block_hashes.len())`，回答：如果调用方传了长度不等的两个列表，release 构建下会发生什么？
3. **需要观察的现象**：纯阅读，无运行现象。
4. **预期结果**：36 个 `LayerSave`；id 条目 36 × 5 = 180 个，hash 克隆同样 36 × 5 份；`block_ids.clone()` 执行 36 次。长度不等问题：`debug_assert` 只在 debug 构建报警，release 下静默通过，错位配对会把「块 3 的数据」存到「块 4 的 hash」下——这正是它用 `debug_assert` 而非返回 `Err` 的原因：契约型不变量交给测试构建抓，运行时不为它付路径。
5. 待本地验证项：无。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `build_saves` 里 hash 不需要跨层区分，而设备数据天然就区分了？

**答案**：内容寻址的粒度是「逻辑块」而非「层的块」——一个逻辑块（16 个 token 的前缀）的键是它的链式哈希，对所有层一视同仁；命中时也总是整块（所有层一起）恢复。设备数据之所以天然区分，是因为每层 arena 基址不同，pegaflow 按 `(layer_name, block_id)` 寻址，同一块号在不同层落在不同显存位置。

**练习 2**：如果把 `i32 → usize` 的转换挪到 `save` 的公共入口而 `build_saves` 收 `&[usize]`，会有什么代价？

**答案**：功能等价，但会把「两个世界的块号约定差异」从单一私有函数扩散到多个入口（`save`、`save_blocking`、`submit_save` 三处签名都得改），每个新入口都要记得转换一次。现在的设计是「边界处转换一次，crate 内部统一约定」——与 lib.rs 对 pegaflow 类型的再导出（u1-l1）同一种「边界收窄」思路。

---

### 4.2 `submit_save`：spawn + oneshot + keep_alive 的 drop 时机（两条契约）

#### 4.2.1 概念说明

`submit_save` 是 save 链路的本体：把保存任务提交到 pegaflow 的 runtime 后**立即返回**一个 `SaveHandle`。它的文档注释里写着两条必须由调用方维护的契约——本 crate 自己无法强制，因为**它不拥有 pegainfer 的计算流**。

**ORDERING CONTRACT（排序契约）——什么时候的块才能存？**

pegaflow 的 D2H 拷贝跑在自己的流上，与 pegainfer 写 KV 的计算流没有依赖。也就是说：提交保存的那一刻，pegaflow 不会（也无法）等计算流上还没落笔的 attention 写入。如果调用方在「产生这些 KV 的 forward 步尚未同步」时就提交保存，D2H 可能读到**半个旧半个新**的撕裂数据。所以契约要求：只保存「写入已全部完成」的块——在 executor 里，这意味着块已封口（seal），且调用点在 forward 步同步之后（post-step-sync）。

**REUSE CONTRACT（重用契约）——提交之后块还能动吗？**

拷贝是异步的，发生在 `submit_save` 返回**之后**。从提交到 D2H 落地之间存在一个窗口：如果这期间源块被分配器回收、分给新请求并被覆写，D2H 抓到的就是**错请求的 KV**，而且它会被持久化在**旧请求的 hash** 名下——比撕裂更糟，因为数据在「字节级」上是完整自洽的，只是属于别人。`keep_alive` 参数就是封这个窗口的：它是一个不透明载荷（`G: Send + 'static`），典型形态是源块的 allocator guard 集合；它被 move 进 spawn 出来的任务，**只在保存任务结束时才 drop**——guard 不 drop，块就不可回收，窗口物理上不存在。

两条契约合起来可以用一个窗口条件表达。记 D2H 的读窗口为 \( (t_{\text{start}}, t_{\text{end}}) \)，块的「可变窗口」有两段——本请求计算流的写入期 \( (t_{\text{write}}, t_{\text{sync}}) \) 和块被重用后的覆写期 \( (t_{\text{reuse}}, \infty) \)，则安全条件是：

\[ (t_{\text{start}}, t_{\text{end}}) \cap \big( (t_{\text{write}}, t_{\text{sync}}) \cup (t_{\text{reuse}}, \infty) \big) = \varnothing \]

ORDERING CONTRACT 保证 \( t_{\text{start}} > t_{\text{sync}} \)；REUSE CONTRACT（keep_alive）保证 \( t_{\text{reuse}} > t_{\text{end}} \)。

#### 4.2.2 核心流程

```
submit_save(block_ids, block_hashes, keep_alive)
  │
  ├─ debug_assert: 两列表等长
  ├─ 空输入 → 直接返回 SaveHandle::settled(Ok(()))   // 快速路径
  │
  ├─ saves = build_saves(...)                        // 跨层扇出
  ├─ 克隆 Arc<PegaEngine>、instance_id、device_id 进任务
  ├─ oneshot 通道 (tx, rx)
  │
  ├─ host.runtime.spawn(async move {
  │     result = engine.batch_save_kv_blocks_from_ipc(...).await   // 异步 D2H
  │     if tx.send(result) 落空 → log::warn("best-effort")        // 日志兜底
  │     drop(keep_alive)                                           // ★ D2H 落地后才释放 pin
  │ })
  │
  ├─ write_barrier 锁内: pending_saves 清掉已完成的，push 新句柄   // 为 flush 屏障记账
  └─ 返回 SaveHandle::from_rx(rx)
```

注意三个动作的**顺序**：`send` 在 `drop(keep_alive)` 之前——观察者先收到「完成」，块的 pin 才释放；`drop(keep_alive)` 又在 spawn 的任务体内，而不是 `submit_save` 的调用点——这就是「guard 必须活过整个异步 D2H」的落地方式。

#### 4.2.3 源码精读

契约的权威表述在文档注释里。[engine.rs:L231-L237](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L231-L237) 是 ORDERING CONTRACT：pegaflow 的 D2H 在自己的流上、与计算流无依赖，调用方只能保存「产生它的 forward 步已同步」（即 executor 里封块时刻、post-step-sync）的块，存一个 attention 写入还在途的块会读到撕裂数据，且这个连接器无法代为强制（它不拥有计算流），必须由接线方维护。[engine.rs:L239-L245](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L239-L245) 是 REUSE CONTRACT：拷贝在返回之后异步读 GPU 块，块必须保持稳定到拷贝落地；`keep_alive` 作为不透明载荷（如源块的 allocator guard）被 spawn 出的保存任务持有、只在任务结束时 drop——否则调用方的块可能在在途 D2H 之下被回收覆写，把错的 KV 快照以旧 hash 持久化；只有当块由别处担保整个保存期稳定时才可传 `()`。

函数主体 [engine.rs:L246-L282](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L246-L282)，关键四段：

```rust
let (tx, rx) = oneshot::channel();
let handle = self.host.runtime.spawn(async move {
    let result = engine
        .batch_save_kv_blocks_from_ipc(&instance_id, TP_RANK, PP_RANK, device_id, saves)
        .await;
    // A dropped receiver is the fire-and-forget path: the failure is
    // ours to log, nobody else will see it.
    if let Err(unobserved) = tx.send(result)
        && let Err(e) = unobserved
    {
        log::warn!("pegaflow save failed (best-effort): {e}");
    }
    // Release the source-block pins only now the D2H has landed; before
    // this point the blocks must not be reused (see REUSE CONTRACT).
    drop(keep_alive);
});
```

（节选自 [engine.rs:L260-L275](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L260-L275)）

- 空输入快速路径在 [engine.rs:L253-L255](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L253-L255)：`SaveHandle::settled(Ok(()))` 直接返回一个已完成的句柄，不惊动 runtime。
- 日志兜底（上段 `if let Err(unobserved)` 分支）：receiver 已被 drop 时 `send` 落空，失败「是我们的责任去记日志，没人会看到它」——这是 fire-and-forget 的错误出口，4.3 详述。
- `drop(keep_alive)` 的位置（[engine.rs:L272-L274](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L272-L274)）：在 `batch_save` 完成之后、任务收尾处。即使 receiver 早被 drop，任务照常跑到这一行——承接 u2-l5 的结论「drop 句柄只是分离观察者」。
- 句柄登记进 flush 屏障的记账（[engine.rs:L276-L280](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L276-L280)）：`retain(!is_finished)` 先清掉已完成的，再 push 新的——pending 列表始终只含真正在途的 save。这段是 u2-l8 flush 屏障的钩子，本讲只需知道「每个 save 都会记账」。

句柄侧，[handle.rs:L24-L26](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L24-L26) 定义 `SaveHandle = OffloadHandle<()>`，且是 `pub(crate)`——**crate 外根本拿不到 save 句柄**，外部世界只剩 `save`（不关心结果）和 `save_blocking`（同步等）两个入口。这是「可见性即 API 设计」的又一次应用（u2-l1 讲过同款思路）。`handle.rs` 模块文档 [handle.rs:L1-L7](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/handle.rs#L1-L7) 同时声明：drop 句柄分离的是观察者而非 I/O，底层任务继续跑、keep_alive 载荷继续被持有，直到它自行落地。

最后看真实调用方如何满足两条契约。[executor.rs:L1836-L1877](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1836-L1877) 是 qwen3 executor 的 `save_sealed_blocks`：

- **ORDERING 的满足点**在其文档注释（[executor.rs:L1832-L1835](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1832-L1835)）：紧跟 `apply_prefill` / `apply_decode` 之后调用是安全的，因为产生该步的 token 读回已经同步了计算流——封好的 KV 已全部落笔。
- **REUSE 的满足点**在 pins 的构造（[executor.rs:L1864-L1876](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1864-L1876)）：`assigned_block_guards()` 取出全部已封块的 guard 后 `skip(*cursor)` 只留本次新封的那段，1:1 对齐待存块列表，作为 `keep_alive` 传给 `.save(&block_ids, &block_hashes, pins)`。注释写明动机：为异步 D2H 的整段时长精确 pin 这些块，防止已结束的请求把 slot 交给新请求、在拷贝落地前覆写它。

#### 4.2.4 代码实践

本讲的主实践：**用模拟代码证明 guard 的 drop 时机，并推演无 guard 的静默腐蚀**。`submit_save` 是私有函数，我们在 crate 外复刻它的骨架（以下为**示例代码**，非项目原有代码；不依赖任何第三方库，单文件即可运行）。

1. **实践目标**：亲眼看到「keep_alive 在 D2H 落地之后才 drop」，并用时序图讲清无 guard 场景下错误发生的确切时间点与静默性。

2. **操作步骤**：

把下面的代码存成 `guard_demo.rs`（放在仓库外任意目录，比如 `/tmp`），用 `rustc guard_demo.rs -o guard_demo && ./guard_demo` 运行：

```rust
// 示例代码：复刻 engine.rs::submit_save 的 spawn + oneshot + keep_alive 骨架
use std::sync::mpsc;

/// 模拟 kvbm 的 allocator guard：Drop 时才把块归还池子
struct BlockGuard { block_id: i32 }
impl Drop for BlockGuard {
    fn drop(&mut self) {
        println!("[guard]    release block {} — slot 从此刻起才可被重分配", self.block_id);
    }
}

/// 模拟 pegaflow 的异步 D2H：提交后在“自己的流”上慢速搬运
fn fake_batch_save(delay_ms: u64) -> Result<(), String> {
    std::thread::sleep(std::time::Duration::from_millis(delay_ms));
    println!("[worker]  D2H landed（块内容已离开显存）");
    Ok(())
}

/// 与 submit_save 同构：spawn → 干活 → send → drop(keep_alive)
fn submit_save<G: Send + 'static>(tx: mpsc::Sender<Result<(), String>>, keep_alive: G) {
    std::thread::spawn(move || {
        let result = fake_batch_save(100); // D2H 发生在返回之后
        let _ = tx.send(result);
        drop(keep_alive); // ← 与 engine.rs:L274 相同：落地之后才释放 pin
    });
}

fn main() {
    let (tx, rx) = mpsc::channel();
    let guard = BlockGuard { block_id: 7 };
    submit_save(tx, guard); // guard 所有权移交给后台任务
    println!("[main]    submit_save 已返回，此刻块 7 仍被 pin 着");
    let _ = rx.recv();
    std::thread::sleep(std::time::Duration::from_millis(50)); // 等 worker 打完 release
    println!("[main]    done");
}
```

3. **需要观察的现象**：三行日志的**顺序**——`submit_save 已返回` 在前，`D2H landed` 居中，`release block 7` 最后。也就是说 guard 的释放严格晚于数据落地。

4. **预期结果**：

```
[main]    submit_save 已返回，此刻块 7 仍被 pin 着
[worker]  D2H landed（块内容已离开显存）
[guard]    release block 7 — slot 从此刻起才可被重分配
[main]    done
```

（末尾 sleep 只为让 release 日志稳定先于进程退出打印。）对照 [engine.rs:L261-L275](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L261-L275)：真实代码里这个顺序由 spawn 的任务体保证，而与调用方是否还持有 `SaveHandle` 无关。

5. **推演题（文字 + 时序图回答）**：如果调用方**不持 keep_alive**（传 `()` 但块又不归别处担保），请求 A、B 交替使用同一个 GPU slot 3，具体在哪个时间点出什么错？为什么是静默的？

```
t0  请求 A 分到 slot 3，forward 写入 KV，token 键哈希链 → 块 hash = H_A
t1  A 的步骤结束、块封口；executor 提交 save([3], [H_A])，D2H 启动
t2  D2H 在途（毫秒级窗口开始）──────────────────────────────┐
t3  请求 A 生成完毕，slot 3 「释放」（无 guard：立即回池）      │ 窗口
t4  请求 B admission，分到 slot 3，开始写自己的 KV            │
t5  D2H 仍在读 slot 3 —— 读到 A/B 混合的内容  ◄──────────────┘
t6  拷贝完成：这批「错的字节」以 H_A 为键写入 host 池并注册
t7  未来的任意请求 C 前缀命中 H_A → load 拉回错 KV → logits 静默走样
```

错误发生在 **t5**（也可能在 t4–t6 之间任意时刻，取决于覆写进度——最坏是 t6 时刻已 100% 是 B 的数据）。它**静默**的原因有三层：D2H 本身「成功」（DMA 不校验内容归属）；H_A 是 A 的 token 的合法哈希，键完全正确，任何一层都不会发现「键下的值属于别人」；最终症状只是生成质量漂移，无报错、无 panic、无日志，且可能距案发已过去很久——正是 engine.rs 注释里「persist it under the old hash」描述的场景。

6. 待本地验证项：无——示例不依赖 GPU 与项目代码，任何有 rustc 的机器可跑。

#### 4.2.5 小练习与答案

**练习 1**：qwen3 为什么传 `Vec<KvBlockGuard>` 而不是把整个请求的 KV 状态传进去当 keep_alive？

**答案**：keep_alive 的粒度只需覆盖「本次正在 D2H 的源块」。`save_sealed_blocks` 用 `skip(*cursor)` 把 guard 集合裁剪到与待存块 1:1 对齐（[executor.rs:L1867-L1871](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1867-L1871)）：更早的块早已存完，它们的 guard 若还被持有，就会白白压住 slot、降低池子周转率。pin 得越准，并发越高。

**练习 2**：ORDERING 和 REUSE 两条契约分别防「撕裂」和「错主」，都导致「错字节进 host 池」。它们能合并成一条契约吗？

**答案**：不能干净地合并，因为它们约束的时间段不同且由不同的机制保证：ORDERING 约束的是「提交时刻不得早于计算流同步」——这是一个调用**时机**问题，本 crate 无法观测计算流；REUSE 约束的是「提交之后到 D2H 落地之间块不得被重用」——这是一个**所有权**问题，靠 guard 的生命周期解决。前者只能靠调用点纪律（文档契约），后者可以由类型系统部分承载（把 guard 类型作为参数），所以注释放在一起、机制却分开。

**练习 3**：`submit_save` 的空输入快速路径为什么不 spawn 一个立刻完成的任务，而要直接返回 `settled(Ok(()))`？

**答案**：等价的「spawn 即完成」要多付一次任务调度、一次 oneshot 通道创建，还要在 `write_barrier` 里登记一个立刻就 settle 的僵尸句柄。`settled` 构造器（u2-l5 讲过）直接给一个已完成的句柄，零开销且语义相同——「这批没有块可存，天然成功」。

---

### 4.3 三种形态：`save` / `save_blocking` 与 fire-and-forget 的日志兜底

#### 4.3.1 概念说明

`submit_save` 是私有本体，对外暴露两种包装，对应两类调用方：

- **`save`（fire-and-forget）**：提交后立刻 drop 句柄。调用方完全不在乎结果——丢一次 save 只损未来一次命中机会（u1-l2 的时序红线）。生产路径（qwen3 executor）用它。
- **`save_blocking`（submit-then-wait）**：提交后原地 `wait()`。给同步调用方（测试、非流水线代码）：返回即 D2H 落地，**源块立刻可以重用**，且错误会浮出来。

「fire-and-forget 的日志兜底」指的是 `save` 路径上一个不易察觉的联动：`save` drop 了句柄 → oneshot 的 receiver 消失 → 后台任务里 `tx.send(result)` 落空返回 `Err(unobserved)` → 若结果本身也是 `Err`，没人会看到它，engine.rs 就替所有人记一条 `log::warn!("pegaflow save failed (best-effort)")`。错误不丢失、也不上抛——它变成了日志里的一行。

#### 4.3.2 核心流程

```
save(block_ids, hashes, keep_alive)          // 生产路径
  └─ drop(submit_save(...))                   // 句柄即刻消失
       └─ 后台任务完成时 send 落空
            └─ 若 result 是 Err → log::warn（唯一错误出口）

save_blocking(block_ids, hashes)             // 测试 / 同步调用方
  ├─ assert_outside_runtime("save_blocking")  // 防在 tokio runtime 内调用
  ├─ submit_save(block_ids, hashes, ())       // 不需要 keep_alive：wait 返回前块自然不被重用
  └─ .wait()                                  // 阻塞到 D2H 落地；错误上抛 Result
```

| 维度 | `submit_save`（私有） | `save`（pub） | `save_blocking`（pub） |
|------|---------------------|---------------|----------------------|
| 可见性 | crate 内 | 公开 | 公开 |
| 返回 | `SaveHandle` | `()` | `Result<(), EngineError>` |
| keep_alive | 调用方提供 | 调用方提供 | 固定 `()` |
| 错误去向 | 由句柄观察者决定 | 日志兜底（warn） | 上抛给调用方 |
| 何时可重用源块 | poll/wait 到 Ok 之后 | D2H 落地（guard 释放） | **返回即**可重用 |
| 典型调用方 | crate 内部 / flush 记账 | qwen3 executor（生产） | `tests/cpu_roundtrip.rs` |

`save_blocking` 的 keep_alive 传 `()` 是安全的：`wait()` 会把当前线程挂起直到 D2H 落地，期间调用方不可能去重用块——同步等待本身就是最强的 pin。这也呼应 `submit_save` 文档注释的最后一句「Pass `()` only when the blocks are owned elsewhere for the whole save」（[engine.rs:L245](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L245)）。

#### 4.3.3 源码精读

[engine.rs:L284-L294](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L284-L294) —— `save` 全文只有一行实质：`drop(self.submit_save(block_ids, block_hashes, keep_alive))`。文档注释点明：任何失败（pinned 池满、拷贝错误）只会被记日志，绝不上抛；`submit_save` 的两条契约同样适用。注意这一 drop 触发的正是 4.2 讲过的链：句柄消失 → `send` 落空 → `log::warn` 兜底（[engine.rs:L267-L271](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L267-L271)）。

[engine.rs:L296-L306](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L296-L306) —— `save_blocking`：先过 [engine.rs:L51-L60](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L51-L60) 的 `assert_outside_runtime` 守卫（在 tokio runtime 内部 block 另一个 runtime 会得到一句难懂的 tokio panic；该守卫把这种误用变成响亮而具体的 debug 断言），再 `submit_save(..., ()).wait()`。模块文档（[engine.rs:L10-L13](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L10-L13)）对这类阻塞入口的定位是「给测试和非流水线调用方的薄包装」。

调用方印证：

- 生产 fire-and-forget：[executor.rs:L1876](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1876) 的 `.save(&block_ids, &block_hashes, pins)`。
- 同步包装：[cpu_roundtrip.rs:L110](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L110) 的 `engine.save_blocking(&src_ids, &hashes).expect("save")`——测试需要错误浮出且后续 load 依赖数据已落地，两个诉求都指向 `save_blocking`。

#### 4.3.4 代码实践

grep 验证型实践（无需 GPU）：

1. **实践目标**：确认「生产走 `save`、测试走 `save_blocking`」的分工在当前代码库里真实成立，并学会区分同名方法与无关调用。
2. **操作步骤**：在仓库根目录依次运行——

   ```bash
   grep -rn --include='*.rs' -E '\.save\(&|save_blocking' pegainfer-qwen3/src pegainfer-kv-offload
   ```

   对照输出逐条分类：哪些行是 `OffloadEngine` 的调用？哪些是别的类型（如 qwen35 的 `recurrent_store.save`）碰巧同名？
3. **需要观察的现象**：`OffloadEngine::save` 的生产调用点只有 `pegainfer-qwen3/src/executor.rs:1876` 一处；`save_blocking` 只出现在 `pegainfer-kv-offload/tests/cpu_roundtrip.rs:110`；engine.rs 内部则只有 `submit_save` 的定义与两处包装调用。
4. **预期结果**：与上面分类一致；同时你会看到 grep 会捞出 qwen35 里与 KV offload 无关的 `.save(`（recurrent 状态存储）——这正是「按方法名全局搜索要先看接收者类型」的教训。
5. 待本地验证项：无（grep 本身就是验证）。

#### 4.3.5 小练习与答案

**练习 1**：`save_blocking` 为什么不需要（也没有）`keep_alive` 参数？

**答案**：它的契约由同步语义整体保证——`wait()` 挂起调用线程直到 D2H 落地，期间调用方没有机会去释放/重用源块，同步等待本身就是 pin。这也反证了 keep_alive 参数存在的唯一理由：`save` 提交后立刻返回，调用方**有机会**在 D2H 落地前重用块。

**练习 2**：如果把 `save` 改成返回 `SaveHandle`（即把 `SaveHandle` 改为 pub）并让 qwen3 自己决定何时 drop，会怎样？

**答案**：功能上可行（qwen3 反正不 poll 它），但把「这个调用点不在乎结果」的意图从类型签名里抹掉了：后续维护者会不确定该不该 poll、会不会泄漏 lease 之类的资源（对比 QueryHandle 提前 drop 会滞留 lease 到 TTL，u2-l5）。现在 `save` 返回 `()` 把「fire-and-forget」编码进签名，错用空间更小。

**练习 3**：日志兜底为什么放在后台任务里（`send` 落空分支），而不是放在 `save` 的 drop 处？

**答案**：drop 句柄时保存还没完成，此刻没有任何失败可言——失败发生在 `batch_save` 执行中。只有跑到任务终点的那一方才知道结果，也只在那里才存在「有结果却无人接收」的事实。把兜底放在任务的 `send` 落空分支，是唯一能同时覆盖「失败 + 无人观察」这个组合的位置。

## 5. 综合实践

把本讲全部内容串成一张**带契约标注的 save 事件时序图**。以 qwen3 的一次两轮对话为脚本：

1. **任务**：画一张纵向时间轴，包含以下事件——第 1 轮 prefill 完成（步同步）、封 4 个块、`save_sealed_blocks` 被调用（`build_saves` 扇出、spawn、guard 移交）、D2H 在途、第 1 轮请求 Finished、第 2 轮请求 admission 复用其中 2 个 slot、D2H 落地、guard 释放、句柄被 drop（fire-and-forget）。
2. **要求标注**：
   - ORDERING CONTRACT 在哪个事件对之间被满足（「步同步」必须先于「提交 save」）；
   - REUSE CONTRACT 由哪段持有期保证（「guard 移交」到「D2H 落地后 drop」），并在图上圈出如果 guard 缺失时「第 2 轮覆写」会落入 D2H 窗口的重叠区；
   - fire-and-forget 的错误出口链路（句柄 drop → send 落空 → warn）画成一条旁路。
3. **对照源码自查**：图上每个箭头旁标注对应的源码位置——`build_saves`（[engine.rs:L207](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L207)）、spawn 与 drop 时机（[engine.rs:L261-L275](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L261-L275)）、guard 构造与调用（[executor.rs:L1864-L1876](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1864-L1876)）。
4. **交付**：一张图 + 三五句话，回答「为什么这张图里唯一可能发生的『坏结果』只是日志里一行 warn，而绝不会是错 KV 进 host 池」。参考要点：只要两条契约被维护，错误路径只剩 pegaflow 侧的失败（池满/拷贝错），全部被 fire-and-forget 兜底吃掉；契约破坏属于接线 bug，类型系统不拦、只能靠调用方纪律——这正是契约写在文档注释而非代码里的原因。

## 6. 本讲小结

- `build_saves`（[engine.rs:L205-L220](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L205-L220)）把一组 `(block_id, hash)` 按注册层扇出成 `Vec<LayerSave>`：id 与 hash 跨层不变、只有设备数据随层不同；`i32 → usize` 的块号翻译只在这一个边界做。
- `submit_save`（[engine.rs:L246-L282](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/src/engine.rs#L246-L282)）= spawn + oneshot + keep_alive：提交即返回句柄；`drop(keep_alive)` 位于任务终点、D2H 落地之后——guard 的生命周期就是 REUSE CONTRACT 的物理实现。
- 两条调用方契约：ORDERING（只能在 forward 步同步后保存已封块，否则撕裂）与 REUSE（keep_alive 钉住源块到 D2H 落地，否则错 KV 以旧 hash 持久化）。根源都是「pegaflow 的 D2H 流与 pegainfer 计算流无依赖」，本 crate 不拥有计算流，所以只能靠文档契约 + 接线纪律。
- 三种形态：`submit_save` 私有本体；`save` 公开 fire-and-forget（错误唯一出口是 `send` 落空后的 `log::warn`）；`save_blocking` 同步包装（返回即可重用块、错误上抛、`assert_outside_runtime` 防误用）。`SaveHandle` 是 `pub(crate)`——外部没有第三种选择。
- 生产接线在 qwen3 的 `save_sealed_blocks`：步同步后调用满足 ORDERING，`assigned_block_guards().skip(cursor)` 的精确 pin 满足 REUSE；丢了 save 只损命中，契约失守才会错字节进池。

## 7. 下一步学习建议

save 是「写进 host 池」的半边；下一讲 **u2-l7「query/load 链路」**讲「从 host 池读回来」的半边：`submit_query` 的 Ready/Loading 两态、query lease 的钉住语义、`load` 把第 i 个租约块映射到第 i 个目标块的顺序契约。两半共用 `OffloadHandle` 的收账机制（u2-l5），但时序性格相反——save 尽力而为，load 强排序不阻塞 admission。之后 **u2-l8「flush 屏障」**会回收本讲埋下的钩子：`submit_save` 登记进 `write_barrier.pending_saves` 的那些 `JoinHandle`，正是屏障要逐个等掉的对象。若想看 save 的字节级验收，跳读 u3-l4 的 `tests/cpu_roundtrip.rs` 解剖（其中 [L110-L111](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-offload/tests/cpu_roundtrip.rs#L110-L111) 的 `save_blocking` + `flush_saves` 就是本讲两种形态的串联使用）。
