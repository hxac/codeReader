# KvView 与注意力内核的契约

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 paged attention 内核对「页表 + 序列长度」的硬性契约，以及**多一页**与**少一页**分别会引发什么事故（垃圾 K/V 软失败 vs 越界读硬崩溃）。
- 对任意 `seq_len` 手推 `num_pages` 与 `last_page_len`，并用内核的反推公式 \((\text{num\_pages}-1)\times\text{page\_size}+\text{last\_page\_len}\) 自检。
- 复述从 `RequestKv` 的原始块清单，到 `step_page_indices` 裁剪出精确页表行，到 `KvView`，最终打包成 `KvViewDesc` 的完整链路——并解释为什么视图**绝不**直接使用请求持有的原始块清单。

本讲是 u2-l2「三拍节奏」（schedule → view → apply）中 **view 这一拍**的展开。

## 2. 前置知识

- **页表（page table）**：类比操作系统的虚拟内存分页。每个请求持有一个 `Vec<i32>`，第 `i` 项是序列第 `i` 页对应的物理块号（block id）。结合 u1-l3 学过的页优先布局，页号 `p` 的物理位置就是：基址 + \(p \times \text{page\_stride}\)。
- **注意力内核怎么消费页表**：paged attention 内核（本项目经 FlashInfer 调用，见 `../CLAUDE.md` 架构图）完全不管块的生命周期——分配、共享、回收都是 `BlockPool` 的事。内核每次启动只拿到两样东西：一张页表行和一个序列长度。它按页迭代取 K/V，对每页只读前 \(\min(\text{page\_size}, \text{剩余 token 数})\) 个位置，逐位置算 query·key 再做 softmax。
- **契约（contract）**：跨层调用双方必须始终满足的不变式。本讲的契约是——**页表长度必须恰好覆盖序列长度**。它由 `KvView::new` 的断言强制执行。
- **回顾 u2-l2 的两个伏笔**：
  - 悬空 token：刚采样出来、KV 还没算的 token，所以 decode 视图的 `seq_len` 恒为 `kv_position + 1`。
  - 积极分配（eager allocation）：kvbm 的 `schedule_decode` 在本步 token 恰好填满当前块时会**预取下一块**，于是请求持有的原始块清单可以比 KV 实际需要的多一块。这是本讲 4.3 节的主角。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `src/view.rs` | 本讲主角，仅 102 行：`KvView`（轻量非拥有的逻辑视图）与 `KvViewDesc`（内核元数据束） |
| `src/pool.rs` | 视图的生产者：`prefill_view` / `decode_view` / `speculative_view` 三个视图构造器，以及 `page_indices` / `current_page_indices` / `step_page_indices` 页表裁剪；文件底部的内联单测 `step_page_indices_exact_at_block_boundaries` 记录了 issue #291 的历史 |
| `tests/lifecycle.rs` | 需 CUDA 的集成测试，端到端断言视图的 `seq_len` / `num_pages` / `last_page_len` |
| `src/buffer.rs`（辅助） | `desc()` 携带的物理 buffer 来源：`KvBuffer::layout()` / `buffer()` 访问器 |
| `src/layout.rs`（辅助） | `KvLayout` 纯值几何，`derive(Copy)`，是 `KvViewDesc` 的字段之一 |
| `src/manager.rs`（辅助） | `KvCacheManager::pool()` / `buffer()` 访问器，集成测试经它取料 |

## 4. 核心概念与源码讲解

### 4.1 KvView：页表行的「恰好覆盖」契约

#### 4.1.1 概念说明

`KvView` 是什么：一个只有三个字段的纯数据结构——`page_indices: Vec<i32>`、`seq_len: usize`、`page_size: usize`。它不拥有任何 GPU 资源，每次 forward 前由 `RequestKv` 现场构造，用完即弃。

解决什么问题：内核需要知道「这个请求**此刻**的 KV 长什么样」，但完全不需要知道块从哪来、是否被共享、何时回收。`KvView` 就是把逻辑层（块池生命周期）与内核层（只读快照）隔开的那个极简接口。

为什么需要硬契约：内核**用页数和末页长度反推序列长度**。页表行多一页，内核就把多出的那页当作序列的一部分，对页里的垃圾字节（别的请求或陈旧数据残留的 K/V）计算 softmax 权重——输出悄悄出错，不崩溃，极难排查；页表行少一页，内核按页表索引取数时越过序列真实需要的页，发生越界读——崩溃或数据损坏。这就是 `KvView::new` 文档注释里引用的 #291。

#### 4.1.2 核心流程

Rust 侧（视图构造时）的正向推导：

\[
\text{num\_pages} = \left\lceil \frac{\text{seq\_len}}{\text{page\_size}} \right\rceil
\]

\[
\text{last\_page\_len} =
\begin{cases}
0 & \text{seq\_len} = 0 \\
\text{page\_size} & \text{seq\_len} \bmod \text{page\_size} = 0 \text{ 且 } \text{seq\_len} > 0 \\
\text{seq\_len} \bmod \text{page\_size} & \text{ otherwise}
\end{cases}
\]

当 \(\text{seq\_len} \ge 1\) 时它等价于简洁的一行：\(\text{last\_page\_len} = ((\text{seq\_len}-1) \bmod \text{page\_size}) + 1\)。

内核侧的反向推导（这正是契约的来源）：

\[
\text{seq\_len} = (\text{num\_pages}-1)\times\text{page\_size} + \text{last\_page\_len}
\]

于是构造断言为：`page_indices.len() == seq_len.div_ceil(page_size)`（`div_ceil` 是 Rust 标准库的上取整除法，即 \(\lceil a/b \rceil\)）。

以 `page_size = 16` 为例查表自检：

| seq_len | num_pages | last_page_len | 反推验证 \((n-1)\times16+l\) |
|---------|-----------|---------------|------------------------------|
| 0       | 0         | 0             | —（空序列） |
| 10      | 1         | 10            | \(0\times16+10=10\) ✓ |
| 16      | 1         | **16**（不是 0）| \(0\times16+16=16\) ✓ |
| 17      | 2         | 1             | \(1\times16+1=17\) ✓ |
| 31      | 2         | 15            | \(16+15=31\) ✓ |
| 32      | 2         | **16**        | \(16+16=32\) ✓ |
| 33      | 3         | 1             | \(2\times16+1=33\) ✓ |

#### 4.1.3 源码精读

结构体本体——三个字段，`#[derive(Clone)]`，可廉价克隆给多层/多内核使用：

[src/view.rs:L7-L16](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L7-L16) —— 文档注释说明它「在每次 forward 前由 `SchedulableSequence` 的已分配块 ID 构造，块生命周期由 `BlockManager` 外部管理」，字段只有 `page_indices` / `seq_len` / `page_size`。

构造函数与契约断言——本讲最重要的一段：

[src/view.rs:L18-L34](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L18-L34) —— `KvView::new` 的文档注释直接写明契约与违反后果：内核按 \((\text{num\_pages}-1)\times\text{page\_size}+\text{last\_page\_len}\) 推导序列长度，「多一页会让它们读到序列之后的垃圾 K/V，少一页会越界（#291）」；随后 `assert_eq!(page_indices.len(), seq_len.div_ceil(page_size), ...)` 把契约变成运行时硬检查。

`last_page_len` 的分段推导，与 4.1.2 的数学一一对应：

[src/view.rs:L44-L51](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L44-L51) —— 空序列返回 0；否则取 `seq_len % page_size`，余数为 0 说明末页**恰好填满**，返回 `page_size` 而不是 0，否则返回余数。

只读访问器（内核侧与测试都靠它们，没有任何可变方法）：

[src/view.rs:L36-L42](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L36-L42) —— `seq_len()` 与 `num_pages()`（就是 `page_indices.len()`）。

[src/view.rs:L53-L55](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L53-L55) —— `page_indices()` 返回 `&[i32]` 切片借用。页表元素是 `i32` 而非 `usize`，因为内核 API 直接吃 i32 页表；这也是 `BlockPool::padding_block_id()` 返回 `i32` 的原因——CUDA Graph 捕获的页表用它直填哑页号（见 [src/pool.rs:L116-L118](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L116-L118)，u2-l1 已讲）。

#### 4.1.4 代码实践

1. **实践目标**：用纯逻辑测试（无需 GPU、无需 CUDA）验证「恰好覆盖」契约的正反两面——17 个 token 配 2 页合法；10 个 token 配 2 页必须 panic。
2. **操作步骤**：在 `pegainfer-kv-cache/tests/` 下新建集成测试文件 `kv_view_contract.rs`（不改动任何源码文件；`KvView` 从 crate 公共 API 导出）。以下为**示例代码**（非项目原有代码）：

   ```rust
   use pegainfer_kv_cache::KvView;

   /// 17 个 token、page_size=16：恰好 2 页，第二页只有 1 个 token。
   #[test]
   fn kv_view_exact_cover_17_tokens() {
       let view = KvView::new(vec![11, 29], 17, 16);
       assert_eq!(view.num_pages(), 2);
       assert_eq!(view.last_page_len(), 1);
       assert_eq!(view.seq_len(), 17);
       // 内核反推公式：序列长 = (页数-1)*page_size + 末页长度
       assert_eq!((view.num_pages() - 1) * 16 + view.last_page_len(), 17);
   }

   /// 2 页配 10 个 token：违反恰好覆盖契约，构造必须 panic。
   #[test]
   #[should_panic(expected = "KvView pages must exactly cover")]
   fn kv_view_surplus_pages_panic() {
       let _ = KvView::new(vec![11, 29], 10, 16);
   }
   ```

   然后运行：

   ```bash
   cargo test --release -p pegainfer-kv-cache --test kv_view_contract
   ```

3. **需要观察的现象**：两个测试都通过；第二个测试的 `#[should_panic(expected = ...)]` 捕获到的 panic 消息里包含 `KvView pages must exactly cover seq_len=10 (page_size=16)`（`assert_eq!` 的自定义消息）。
4. **预期结果**：`test result: ok. 2 passed`。⚠️ 待本地验证（我未在此环境运行该命令；断言行为直接读自 `KvView::new` 源码，是确定的）。
5. **契约被破坏时内核会出什么错**（用一段文字解释）：假设某个调度器 bug 传入了 2 页 + `seq_len=10`——内核信任页数，反推出长度 \((2-1)\times16+l\)，即它认为序列至少延伸到第二页；于是注意力会**对第二页里的全部 16 个位置计算 softmax 权重**。那 16 个位置的字节是这块物理页上一次被别的请求写入后残留的陈旧 K/V（垃圾），概率质量被分给这些垃圾键，输出 logits 悄悄错误——模型不崩溃、不报错，只是答非所问，这类软失败在生产上最难定位。反过来，若序列需要 2 页却只给了 1 页，内核要读的位置超出页表行覆盖范围，寻址越过该请求的实际块，发生**越界读**——轻则读到其他请求的 KV（串台），重则触发非法显存访问直接崩溃（#291 记录的就是这类问题）。

#### 4.1.5 小练习与答案

**练习 1**：`page_size=64`，`seq_len=130`，求 `num_pages` 与 `last_page_len`，并用反推公式验证。

答案：\(\lceil 130/64 \rceil = 3\) 页；\(130 \bmod 64 = 2\)，余数非零，`last_page_len = 2`。验证：\((3-1)\times64+2 = 130\) ✓。

**练习 2**：`seq_len=32`、`page_size=16` 时，`32 % 16 == 0`，为什么 `last_page_len` 必须返回 16 而不是余数 0？

答案：余数为 0 的语义是「末页恰好填满」，末页长度就是 `page_size`。若返回 0，内核反推 \((\text{num\_pages}-1)\times\text{page\_size}+0\) 会凭空丢掉整整一页已计算的 KV，注意力漏看 16 个 token，输出错误。

**练习 3**：为什么本讲把「多一页」叫软失败、「少一页」叫硬失败？

答案：多一页时内核仍在合法显存内读，只是把垃圾 K/V 当成了序列尾部——结果错误但进程存活，只能靠输出质量或概率分布异常发现；少一页时内核需要的页号超出页表行，寻址落在请求真实块之外，可能越过池的合法范围触发非法访问崩溃，或读到别的请求数据——错误立即或以更惨烈的方式显形。

### 4.2 KvViewDesc：打包给内核的元数据束

#### 4.2.1 概念说明

`KvView` 只有逻辑三件套，但启动一次 attention 内核还需要**物理与几何信息**：显存基址（`CudaSlice<bf16>`，告诉内核数据在哪）、几何（`KvLayout` 的 `page_stride` / `kv_block_len` / `layer_stride`，告诉内核页号怎么换算成地址、层与 K/V 怎么排布）、页表、序列长度、末页长度。

`KvViewDesc<'a>` 就是把这五样缝成的一束**不可变元数据**。它全部字段私有、只暴露只读访问器，是一个「打包好就不许再动」的传值载体。`desc()` 通过**借用**而非拷贝构造——页表切片与 `CudaSlice` 都是零拷贝引用，生命周期由 `'a` 绑定在 `KvView` 和 `KvBuffer` 的借用上，正好覆盖内核启动（launch）那一刻。

#### 4.2.2 核心流程

```
RequestKv::prefill_view / decode_view / speculative_view
        │  (逻辑层：精确页表行 + seq_len + page_size)
        ▼
      KvView ──desc(&KvBuffer)──▶ KvViewDesc<'a>
                                    │  + KvLayout（几何，Copy 拷贝）
                                    │  + &CudaSlice<bf16>（基址借用）
                                    │  + &[i32]（页表借用）
                                    ▼
                        attention 内核 wrapper（如 FlashInfer）
                        对每页 p：地址 = 基址 + p × page_stride
                        层内按 layer_stride 步进、K 前 V 后（u1-l3）
                        只读 (num_pages-1)×page_size + last_page_len 个位置
```

#### 4.2.3 源码精读

`desc()` 的组装过程：

[src/view.rs:L57-L65](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L57-L65) —— `layout` 用 `*buffer.layout()` **按值拷贝**（`KvLayout` 是 `derive(Clone, Copy)` 的纯值类型，见 [src/layout.rs:L4-L16](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/layout.rs#L4-L16)，7 个 `usize`，拷贝比共享引用更便宜也更符合「临时元数据」语义）；`buffer` 与 `pages` 都是借用；`last_page_len` 在此处算好缓存进结构，内核不必再推。

`KvViewDesc` 结构与全部只读访问器：

[src/view.rs:L68-L75](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L68-L75) —— 五个字段全私有：`layout`、`buffer`、`pages`、`seq_len`、`last_page_len`。

[src/view.rs:L77-L101](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/view.rs#L77-L101) —— 六个访问器 `layout()` / `seq_len()` / `last_page_len()` / `num_pages()` / `page_indices()` / `buffer()`，与 `KvView` 的访问器一一呼应，另加物理的 `buffer()`。

物理信息的来源——`KvBuffer` 的两个访问器：

[src/buffer.rs:L48-L54](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/buffer.rs#L48-L54) —— `layout()` 返回几何，`buffer()` 返回底层 `CudaSlice<bf16>` 的引用。集成测试经 `KvCacheManager::buffer()`（[src/manager.rs:L109-L111](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/manager.rs#L109-L111)）把它递给 `desc()`。

端到端断言每个访问器的集成测试：

[tests/lifecycle.rs:L109-L129](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L109-L129) —— `kv_view_desc_has_correct_layout`：5-token 请求 → `prefill_view(5)` → `view.desc(mgr.buffer())`，断言 `seq_len()==5`、`last_page_len()==5`、`num_pages()==1`、`layout().page_size==16`、`layout().num_layers==1`、`!buffer().is_empty()`、`page_indices().len()==1`——正好覆盖 `KvViewDesc` 的全部访问器。

#### 4.2.4 代码实践

1. **实践目标**：跑通（或走读）`KvViewDesc` 的端到端构造链，理解「借不拷」的设计。
2. **操作步骤**：本测试需要真实 CUDA 设备（`make_manager` 会创建 `CudaContext`）。有 GPU 时运行：

   ```bash
   cargo test --release -p pegainfer-kv-cache --test lifecycle kv_view_desc_has_correct_layout
   ```

   无 GPU 时做源码阅读型实践：对照 [tests/lifecycle.rs:L109-L129](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L109-L129)，在纸上画出 `req.prefill_view(5)` → `view.desc(mgr.buffer())` 的数据流，标注每个 `KvViewDesc` 字段分别来自 `KvView` 还是 `KvBuffer`。
3. **需要观察的现象**：测试输出 `ok`；或源码走读时确认 `layout` 来自 buffer 侧、`pages`/`seq_len`/`last_page_len` 来自 view 侧、`buffer` 来自 buffer 侧。
4. **预期结果**：单测通过（⚠️ 待本地验证；无 GPU 环境下该测试会在 `CudaContext::new` 处 expect 失败，属预期行为）。
5. **附加思考**：`desc()` 为什么借而不拷？——页表可能随请求增长到几十上百项，每次 forward 都要组一次 `desc`，切片借用是零成本；而 `KvLayout` 是 7 个 `usize` 的 `Copy` 类型，拷贝反而最简单。两者的取舍正好示范了「按字段语义选所有权形式」。

#### 4.2.5 小练习与答案

**练习 1**：`KvViewDesc` 为什么要同时携带 `layout` **和** `buffer` 指针？只有页表不行吗？

答案：页表只给页号。内核要把页号换算成显存地址，需要 `page_stride`（页号 → 字节偏移）与 `kv_block_len` / `layer_stride`（页内层与 K/V 寻址），这来自 `layout`；还需要基址，这来自 `buffer`。缺 `layout` 算不出偏移，缺 `buffer` 不知道从哪开始加——两者缺一，页号就只是无意义的整数。

**练习 2**：`KvView` 有 `seq_len()`，`KvViewDesc` 里也存了 `seq_len`，为什么 `desc()` 还要预先算好 `last_page_len` 存进去？

答案：把「按 4.1.2 的分段公式推导末页长度」这件事在 Rust 侧一次算清，内核 wrapper 直接读现成值，避免每个消费者（多层注意力、不同内核后端）各自重推一遍、也避免某处推错破坏反推公式。元数据束的意义就是**一次性把内核要的都备齐**。

**练习 3**：`KvViewDesc<'a>` 的生命周期 `'a` 由谁撑着？如果内核启动是异步的（提交后立刻返回），会出现什么问题？

答案：`'a` 同时绑定 `KvView`（页表切片）与 `KvBuffer`（`CudaSlice` 引用）的借用。若异步启动期间两者任何一个被释放/清空，借用悬空——这正是真实代码里用 `Arc<Inner>` 持有 `CudaSlice`、且模型执行器在整个 forward（含内核 launch）完成前保持 view 存活的原因；`KvBuffer` 的指针稳定性还让 CUDA Graph 捕获与 kv-offload 注册成为可能（下一讲 u2-l4 展开）。

### 4.3 RequestKv 的页表裁剪：current/step_page_indices 与视图的关系

#### 4.3.1 概念说明

`RequestKv` 内部有一份**原始块清单** `page_indices()`——请求当前持有的全部物理块。问题在于：这份清单**可能比 KV 需要多一块**。kvbm 的 `schedule_decode` 按 `need = pending + 1` 计算块数，当本步 token 恰好填满当前最后一块时，会**积极预取下一生成块**（保证下一步不解码不断供）。于是「持有的块」≠「KV 覆盖的块」。

如果把原始清单直接当页表交给内核，就是 4.1 说的「多一页」契约违规。这不是理论风险：[src/pool.rs:L952-L960](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L952-L960) 的测试文档注释记录了两个真实症状——qwen3 的 FlashInfer metadata **在每个块边界多读一页垃圾 K/V**（#291），以及 Kimi DP8 worker 的精确匹配页表检查遇到多出的块后**死锁在 H200 上**。

于是 crate 提供两个裁剪 API，把原始清单修剪成合法页表行：

- `current_page_indices()`：已提交 KV 的精确页表（截到 \(\lceil \text{kv\_position} / \text{block\_size} \rceil\)）。
- `step_page_indices(new_tokens)`：**本步之后**状态的精确页表行（截到 \(\lceil(\text{kv\_position}+new\_tokens)/ \text{block\_size}\rceil\)）——三个视图构造函数全部经由它，绝不直接用原始清单。

#### 4.3.2 核心流程

三个入口的量化关系（`bs` 为 `block_size`，即 `page_size`）：

| 构造器 | 页表来源 | seq_len | 页表行长度 |
|--------|----------|---------|------------|
| `prefill_view(prompt_len)` | `step_page_indices(prompt_len)` | `kv_position + prompt_len` | \(\lceil(\text{kv\_pos}+P)/bs\rceil\) |
| `decode_view()` | `step_page_indices(1)` | `kv_position + 1`（悬空 token） | \(\lceil(\text{kv\_pos}+1)/bs\rceil\) |
| `speculative_view(n)` | `step_page_indices(n)` | `kv_position + n` | \(\lceil(\text{kv\_pos}+n)/bs\rceil\) |

裁剪伪代码：

```
step_page_indices(new_tokens):
    assert new_tokens > 0                # 一个 forward 步至少追加 1 个 token
    kv_tokens = kv_position + new_tokens # 本步后的 KV 长度
    pages = 原始块清单
    pages.truncate(ceil(kv_tokens / block_size))   # 砍掉积极多配的尾巴
    return pages
```

`current_page_indices` 与 `step_page_indices(1)` 在 decode 步里的关系：两者只在**本步新 token 跨入新块**时不同（\(\lceil(\text{kv\_pos}+1)/bs\rceil > \lceil\text{kv\_pos}/bs\rceil\) 恰在块边界发生），其余时刻相等。

#### 4.3.3 源码精读

原始清单（私有，故意不外泄）：

[src/pool.rs:L598-L608](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L598-L608) —— `page_indices()` 返回请求的全部块 ID；文档注释明确警告「可以比 KV 需要多一块（见 `step_page_indices`）」。

已提交 KV 的精确页表：

[src/pool.rs:L610-L616](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L610-L616) —— `current_page_indices()` 把原始清单截断到 \(\lceil \text{kv\_position}/\text{block\_size} \rceil\)，供 offload / 恢复等「只看已落账 KV」的路径使用。

本步页表行——视图的唯一数据源：

[src/pool.rs:L618-L631](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L618-L631) —— `step_page_indices(new_tokens)` 的文档注释完整交代了动机：kvbm 的 `schedule_decode` 会在 token 填满当前块时积极分配下一生成块，「用原始清单建的页表会让内核看到比实际更长的序列——任何交给 forward 的每步页表行都必须用这个函数」。

三个视图构造器，全部走 `step_page_indices`：

[src/pool.rs:L477-L491](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L477-L491) —— `prefill_view(prompt_len)`：文档注释强调视图的 `seq_len = kv_position + prompt_len` 是「推进后状态」（FlashInfer attention metadata 期望的口径），且页表行必须是 `step_page_indices` 的精确产物、**never the raw block holdings**。

[src/pool.rs:L493-L501](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L493-L501) —— `decode_view()`：同样契约，`seq_len = kv_position + 1`。

[src/pool.rs:L503-L515](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L503-L515) —— `speculative_view(num_draft_tokens)`：投机验证步覆盖悬空 token + 草稿候选，视图盖住步后 KV 范围，`apply_speculative` 随后只提交被接受前缀（u2-l2）。

守护这一契约的内联单测（纯逻辑、无 GPU）：

[src/pool.rs:L952-L960](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L952-L960) —— 测试文档注释即 #291 事故档案：积极分配使原始清单在每个块边界超出 \(\lceil\text{kv\_tokens}/\text{block\_size}\rceil\)，原始清单直达 Kimi DP8 worker 的精确匹配页表检查导致死锁、并让 qwen3 的 FlashInfer metadata 每逢块边界多读一页垃圾。

[src/pool.rs:L961-L1008](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L961-L1008) —— `step_page_indices_exact_at_block_boundaries` 扫了 10 种 prompt 长度（含 16/17/31/32/33 等边界值）× 23 个 decode 步，逐一断言页表行长度与视图页数精确；其中 [L994-L998](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L994-L998) 正是 4.1.2 的内核反推公式 \((\text{num\_pages}-1)\times16+\text{last\_page\_len} = \text{kv\_position}+1\)，[L1003-L1007](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L1003-L1007) 反向断言「原始清单确实发生过超出」（金丝雀：若 kvbm 哪天不再积极多配，裁剪与该测试即可退役）。

#### 4.3.4 代码实践

1. **实践目标**：运行守护契约的现有单测，并手推一个具体请求的页表演变。
2. **操作步骤**：先运行纯逻辑单测（`BlockPool::new` 不触碰 CUDA，无 GPU 也能跑）：

   ```bash
   cargo test --release -p pegainfer-kv-cache --lib step_page_indices_exact_at_block_boundaries -- --nocapture
   ```

   然后手算：`block_size=16`、prompt 17 个 token。填下表（答案已给出，先遮住自己推）：

   | 时刻 | kv_position | 视图 | seq_len | num_pages | last_page_len |
   |------|-------------|------|---------|-----------|---------------|
   | prefill（schedule 后取 `prefill_view(17)`） | 0 | prefill | 17 | \(\lceil17/16\rceil=2\) | 1 |
   | apply_prefill 后第一次 decode（`decode_view()`） | 17 | decode | 18 | \(\lceil18/16\rceil=2\) | 2 |
   | 再 decode 14 步后（`decode_view()`） | 31 | decode | 32 | \(\lceil32/16\rceil=2\) | **16**（填满） |
   | 再 decode 1 步（`decode_view()`） | 32 | decode | 33 | \(\lceil33/16\rceil=3\) | 1（跨块，原始清单比它多 1 块） |
3. **需要观察的现象**：单测输出 `ok`；手推表中最后一行正是积极分配发生的时刻——此刻 `page_indices()`（私有原始清单）长度为 4，而 `step_page_indices(1)` 裁到 3。
4. **预期结果**：`step_page_indices_exact_at_block_boundaries ... ok`（⚠️ 待本地验证）。手推结果与上表一致。
5. **回答一个问题**：金丝雀断言 [L1003-L1007](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L1003-L1007) 为什么要求 `raw_overshoots > 0` 而不是 `== 0`？——因为测试要证明的前提是「kvbm 确实会积极多配」，如果有一天 kvbm 改成精确分配，`step_page_indices` 的裁剪就变成永真的空操作，这行断言会失败并提醒维护者：「裁剪与这个测试可以退役了」。它守护的不是裁剪的正确性，而是裁剪的**存在理由**。

#### 4.3.5 小练习与答案

**练习 1**：`schedule_decode` 为什么会在块边界「多拿一块」？这是 bug 吗？

答案：kvbm 按 `need = pending + 1` 预留块——当本步 token 恰好填满当前最后一块时，多预留的下一块保证下一步解码不必等分配。这是有意的吞吐优化，不是 bug；bug 是把含预备块的原始清单直接当页表（#291）。

**练习 2**：decode 步中 `current_page_indices()` 与 `step_page_indices(1)` 何时相等、何时差 1？

答案：两者分别截断到 \(\lceil\text{kv\_position}/bs\rceil\) 与 \(\lceil(\text{kv\_position}+1)/bs\rceil\)。当本步新 token 不跨入新块时两者相等；恰在 \(\text{kv\_position}\) 为 `bs` 整数倍（块边界）时，后者比前者多 1。

**练习 3**：`prefill_view` 的文档说 `seq_len` 是「推进后状态」（post-advance），为什么不按「已提交 KV」口径给视图？

答案：因为视图服务于**即将执行**的这次 forward：本步会为 `[kv_position, kv_position+prompt_len)` 的 token 计算 K/V 并写进对应页，attention 同时要读这些新位置（增量 prefill 对新 token 互相注意）。内核元数据（FlashInfer）需要的是本步完成后的总长度，所以 `seq_len = kv_position + prompt_len`，页表也必须盖住这个长度——这正是 4.1 契约里的 `seq_len` 定义。`decode_view` 的 `+1`（悬空 token）同理。

## 5. 综合实践

**任务：无 GPU 的页边界全流程演练。** 把本讲三个模块串起来：用 `BlockPool`（纯逻辑，不需要 CUDA——`tests/restore_commit.rs` 已示范这一点）直接驱动一个请求跨过页边界，对每一步的视图做契约级断言，最后把 4.1 的反推公式批量验证一遍。

1. **操作步骤**：在 `pegainfer-kv-cache/tests/` 下新建 `kv_page_boundary_no_gpu.rs`。以下为**示例代码**（非项目原有代码）：

   ```rust
   use pegainfer_kv_cache::{BlockPool, KvView};

   #[test]
   fn page_boundary_lifecycle_without_gpu() {
       let pool = BlockPool::new(16, 8).expect("pool: need >= 2 blocks");
       let baseline = pool.available_blocks(); // 8 - 1 padding = 7（u2-l1）

       // 16-token prompt 恰好填满第一页；max 2 个输出
       let mut req = pool.new_request(vec![1; 16], 2, None);
       req.schedule_prefill(16, &pool).expect("schedule prefill");

       let pv = req.prefill_view(16);
       assert_eq!((pv.num_pages(), pv.last_page_len()), (1, 16)); // 末页填满：16 而非 0

       req.apply_prefill(42, &pool).expect("apply prefill");
       assert_eq!(req.kv_position(), 16);

       // 首个 decode 步：悬空 token 42 跨入第二页
       req.schedule_decode(&pool).expect("schedule decode");
       let dv = req.decode_view();
       assert_eq!(dv.seq_len(), 17);
       assert_eq!(dv.num_pages(), 2);
       assert_eq!(dv.last_page_len(), 1);
       // 4.1 的内核反推公式
       assert_eq!((dv.num_pages() - 1) * 16 + dv.last_page_len(), 17);

       req.apply_decode(43, &pool).expect("apply decode");
       req.release().expect("release");
       assert_eq!(pool.available_blocks(), baseline); // 块全部归还（u2-l2）
   }

   /// 批量验证恰好覆盖契约与反推公式（纯构造，无池参与）。
   #[test]
   fn reconstruction_formula_across_boundaries() {
       for seq_len in [1usize, 15, 16, 17, 31, 32, 33] {
           let pages: Vec<i32> = (0..seq_len.div_ceil(16) as i32).collect();
           let view = KvView::new(pages, seq_len, 16);
           assert_eq!(
               (view.num_pages() - 1) * 16 + view.last_page_len(),
               seq_len,
               "reconstruction must round-trip at seq_len={seq_len}"
           );
       }
   }
   ```

   运行：

   ```bash
   cargo test --release -p pegainfer-kv-cache --test kv_page_boundary_no_gpu
   ```

2. **需要观察的现象**：两个测试通过；对照 `tests/lifecycle.rs` 的 [page_boundary_crossing](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L81-L107)（同样的 16→17 断言，但经 `KvCacheManager`、需要 CUDA），体会「池 + 视图」这一层完全可以脱离 GPU 测试。
3. **预期结果**：`2 passed`；`release` 后 `available_blocks` 回到 baseline（块进入非活跃池仍计为可分配，与 [tests/lifecycle.rs:L47-L49](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/tests/lifecycle.rs#L47-L49) 的行为一致）。⚠️ 待本地验证。
4. **验收标准**：如果把 `decode_view` 换成 `KvView::new` 配 3 页 + `seq_len=17`，测试应当 panic——你应当能不运行就说出 panic 消息和内核侧的对应事故。

## 6. 本讲小结

- `KvView` 是三字段纯数据（页表行、`seq_len`、`page_size`），是逻辑块池与 paged attention 内核之间唯一的每请求快照接口；构造断言 `page_indices.len() == seq_len.div_ceil(page_size)` 把「恰好覆盖」契约变成硬检查。
- 内核按 \(\text{seq\_len}=(\text{num\_pages}-1)\times\text{page\_size}+\text{last\_page\_len}\) 反推序列长度：多一页 → 垃圾 K/V 参与注意力（软失败）；少一页 → 越界读（硬失败）；两者都是 #291 的教训。
- `last_page_len` 在序列恰为 `page_size` 整数倍时返回 `page_size`（末页填满），空序列返回 0。
- `KvViewDesc<'a>` 把 `KvLayout`（Copy 拷贝）、`&CudaSlice<bf16>`（基址借用）、页表切片、`seq_len`、`last_page_len` 打包成内核元数据束，全私有字段 + 只读访问器，借用零拷贝。
- `RequestKv` 的原始块清单可能含一个积极预取的下一生成块；三个视图构造器一律经 `step_page_indices(new_tokens)` 截断到 \(\lceil(\text{kv\_position}+new\_tokens)/\text{block\_size}\rceil\)，绝不直用原始清单——`step_page_indices_exact_at_block_boundaries` 单测既是回归测试也是 #291 的事故档案。

## 7. 下一步学习建议

- 下一讲 **u2-l4「KvBuffer 与 KvCacheManager：物理层配对」**：本讲 `desc()` 借走的那个 `CudaSlice<bf16>` 从哪来、为什么 `Arc<Inner>` + `alloc_zeros` 一次分配保证指针永不移动、CUDA Graph 捕获与 kv-offload 注册为何依赖这一点，以及 `from_buffer` 在张量并行下的最小公共容量协商。
- 延伸阅读：`src/pool.rs` 底部整个 `#[cfg(test)]` 模块（[L741-L1008](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-kv-cache/src/pool.rs#L741-L1008)）——它示范了如何用纯逻辑测试守护引擎级不变式；以及 FlashInfer 的 paged attention 文档，对照「内核需要什么元数据」与本讲的 `KvViewDesc` 逐字段印证。
