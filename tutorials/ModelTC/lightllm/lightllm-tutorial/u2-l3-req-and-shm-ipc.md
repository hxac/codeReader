# 请求对象与共享内存通信

## 1. 本讲目标

上一讲（u2-l2）我们看清了 HttpServer 如何接收请求、分词、分配 Req 并把请求「索引」交给下游。本讲回答一个更底层的问题：**这个 Req 到底长什么样？它放在哪里？多个进程凭什么能不拷贝大对象就共享它？**

学完本讲你应该能够：

1. 说清楚 `Req` 作为 `ctypes.Structure` 的字段构成，尤其是与**调度**和 **KV 管理**相关的关键字段，以及一个请求从创建到可释放的生命周期状态。
2. 说清楚 `ShmReqManager` 如何用一大块共享内存承载所有 `Req` 槽位，并用「链表式下标分配 + 引用计数」做并发安全的多进程对象管理。
3. 说清楚 `ShmObjsIOBuffer` 的 `write_obj` / `set_ready` / `sub_state` 机制，以及它为什么能在线上只传小对象、把大对象留在共享内存里。

本讲覆盖三个最小模块：**请求对象**、**共享内存**、**进程间通信**。

## 2. 前置知识

在进入源码前，先用通俗语言铺三个基础概念。

### 2.1 为什么是 `ctypes.Structure`

Python 的普通对象（`class Foo: ...`）内存布局由解释器管理，地址会随垃圾回收移动，**无法被另一个进程直接按地址访问**。而 LightLLM 要让 Router、ModelBackend、Detokenization 等多个进程「看同一块内存里的同一个请求」，就必须用一种**布局固定、可按字节寻址**的结构。Python 标准库的 `ctypes.Structure` 正是为此而生：它像 C 结构体一样有确定的字段顺序与字节对齐（`_pack_ = 4` 表示 4 字节对齐），可以安全地「铺」在一段共享内存上。

一个关键能力是 `from_buffer`：给定一段内存缓冲区，`ctypes` 能构造出一个直接指向该缓冲区的结构体数组。于是「同一块共享内存」在不同进程里被 `from_buffer` 一次，就得到了**指向同一物理内存**的视图——一个进程写字段，另一个进程立刻能看到。这是 LightLLM 共享内存通信的物理基础。

### 2.2 共享内存（Shared Memory）

POSIX 共享内存是一段由操作系统管理、可被多个进程同时映射到自己地址空间的内存。Python 标准库 `multiprocessing.shared_memory.SharedMemory` 提供了跨平台封装：创建时给一个 `name` 和 `size`，其他进程用同名 `name` 就能 `link` 到同一段内存。LightLLM 在 [shm_utils.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/shm_utils.py) 里把「不存在则创建、存在则连接」封装成了 `create_or_link_shm`。这也是为什么 LightLLM 启动时必须配 `--shm-size`（见 u1-l2）：所有跨进程状态都住在共享内存里，空间不够就直接启动失败。

### 2.3 对象放共享内存、线上只传索引

这是 u2-l1、u2-l2 已经建立的核心理念，本讲是它的**落地实现**。把它拆成两类「信道」：

| 信道 | 承载内容 | 典型载体 |
| --- | --- | --- |
| 共享内存（常驻） | 大块、常变的状态：`Req` 结构体、prompt token 数组、logprobs | `ShmReqManager`、`ShmArray` |
| 轻量消息（线上） | 小命令：请求索引、中止命令、停止串命中 | zmq、`ShmObjsIOBuffer`（pickle 小对象） |

记住这张表，本讲三个最小模块就是在解释这两行分别怎么实现。

## 3. 本讲源码地图

本讲涉及的关键文件全部在 `lightllm/server/core/objs/` 与 `lightllm/utils/` 下：

| 文件 | 作用 |
| --- | --- |
| [req.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py) | 定义 `Req` 请求对象及其子类 `ChunkedPrefillReq` / `TokenHealingReq`，以及 `FinishStatus`、`PrefixTokenIdsStruct` 等内嵌结构。本讲的「主角」。 |
| [shm_req_manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_req_manager.py) | `ShmReqManager`：在共享内存里开辟 `Req` 槽位数组，提供分配/释放/引用计数接口；内含 `ReqLinkedListManager` 空闲链表。 |
| [shm_objs_io_buffer.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_objs_io_buffer.py) | `ShmObjsIOBuffer`：用一段共享内存 + 原子计数做「单生产者多消费者」命令管道，靠 pickle 传小对象。 |
| [shm_array.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_array.py) | `ShmArray`：把 numpy 数组铺到共享内存上，供 `Req` 存 prompt_ids / logprobs。 |
| [atomic_lock.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/atomic_lock.py) / [atomic_array_lock.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/atomic_array_lock.py) | 跨进程原子锁（基于 `atomics` 的 CAS），保护共享内存里的并发写。 |
| [shm_utils.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/shm_utils.py) | `create_or_link_shm`：统一封装共享内存的「创建或连接」语义。 |

此外会用 [router/manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py) 和 [mode_backend/base_backend.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py) 作为「真实调用方」来印证机制。

## 4. 核心概念与源码讲解

### 4.1 请求对象：Req

#### 4.1.1 概念说明

`Req` 是一次推理请求在 LightLLM 内部的**完整状态容器**。它从 HttpServer 创建，一路被 Router 调度、被 ModelBackend 读写、被 Detokenization 消费，直到最后被释放。因为要在多进程间被直接共享，它被设计成 `ctypes.Structure`，每个字段都有确定的 C 类型与字节宽度。

可以把 `Req` 想成一张「请求档案卡」，上面写着四类信息：

- **身份**：我是谁（`request_id`）、属于哪一组（`group_req_id`）、在共享内存里的第几格（`index_in_shm_mem`）。
- **调度**：输入多长（`input_len`）、是否被暂停（`is_paused`）、是否结束（`finish_status`）、chunked prefill 的大小（`chunked_prefill_size`）。
- **KV 管理**：当前已占用多少 KV（`shm_cur_kv_len`）、命中了多少 prompt cache（`prompt_cache_len`、`cpu_prompt_cache_len`、`disk_prompt_cache_len`）。
- **输出与生命周期**：输出了多长（`shm_cur_output_len`）、输出环形队列（`out_tokens_queue`）、是否可释放（`can_released_mark`、`ref_count`）。

#### 4.1.2 核心流程：一个 Req 的生命周期

一个 `Req` 从生到死大致经历以下阶段（结合 u2-l2 的分发流程看更清楚）：

```
1. HttpServer: alloc_req_index()  → 得到空闲槽位 index_in_shm_mem
2. HttpServer: get_req_obj_by_index(idx) → ref_count += 1，拿到 Req 引用
3. HttpServer: req.init(...)        → 写身份/调度字段，prompt_ids 写入自己的 ShmArray
4. HttpServer → Router: 只把 index 通过 zmq 发过去（不传 Req 本体）
5. Router:    get_req_obj_by_index(idx) → ref_count += 1，调度、改 finish_status 等
6. Router → ModelBackend: 通过 ShmObjsIOBuffer 发轻量命令（含 index）
7. ModelBackend: 按 index 读 Req 字段做推理，写回 shm_cur_kv_len / shm_cur_output_len
8. Detokenization: 按 index 读 out_tokens_queue 解码，置 can_released_mark=True
9. Router: can_release() 成立 → release_req_index(idx)，槽位回收
```

注意第 4、6 步：**线上传递的始终是小命令或 index，Req 本体从未被序列化搬运**，它一直待在共享内存的固定槽位里被原地读写。这就是「对象放共享内存、线上只传索引」的真实样子。

#### 4.1.3 源码精读

`Req` 首先是一个 `_pack_ = 4` 的 `ctypes.Structure`，字段表（`_fields_`）就是它的「内存图纸」：

[req.py:71-128](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L71-L128) 定义了 `Req` 的全部字段。这里挑出与**调度**和 **KV 管理**最相关的几组（行号为上述区间内）：

- `index_in_shm_mem`（L74）：本 Req 在共享内存槽位数组里的下标，是跨进程寻址的「门牌号」。
- `ref_count`（L75）：引用计数，注释特意写明「个人不要操作这个引用计数」——它只能由 `ShmReqManager` 的加锁接口修改（见 4.2）。
- `input_len`（L79）、`chunked_prefill_size`（L100）、`is_paused`（L91）：调度器最关心的输入长度、分块大小、暂停标记。
- `shm_cur_kv_len`（L82）：推理进程记录「自己当前占用了多长的 KV 显存」，调度估算负载时直接读它（见 u4-l3）。
- `prompt_cache_len` / `cpu_prompt_cache_len` / `disk_prompt_cache_len`（L88-L90）：分别记录 GPU、CPU、磁盘三级缓存的命中长度（对应 u4-l2、u6-l4）。
- `shm_cur_output_len`（L83）与 `candetoken_out_len`（L87）：前者记输出长度，后者是「detokenization 进程可以解码的长度」。注释解释了为什么单独用一个字段：为了避免多进程并发访问 `cur_output_len` 时的竞态，单独用 `candetoken_out_len` 传这条信息。
- `finish_status`（L92）：内嵌的 `FinishStatus` 结构体，标记请求是否结束。
- `out_tokens_queue`（L98）：内嵌的 `CircularQueue`，存放解码后的输出片段，是 Detokenization 与下游通信的环形队列。
- `can_released_mark`（L105）：流程末端（通常是 Detokenization）置 True 后，管理进程才真正释放请求。
- `prefix_token_ids`（L101）：仅 token healing 模式使用的前缀 token。

`FinishStatus` 是一个只有 `status` 一个 int 字段的小结构体，但封装了三种状态语义：

[req.py:21-53](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L21-L53) 定义了 `NO_FINISH=0 / FINISHED_STOP=1 / FINISHED_LENGTH=2` 三态，并提供 `is_finished()`、`get_finish_reason()`（返回 `"stop"`/`"length"`）等查询方法。用一个 int 就表达「未结束 / 命中停止串结束 / 达到长度上限结束」，紧凑且跨进程安全。

`Req.init()` 是「填档案卡」的地方，HttpServer 拿到槽位后调用它完成初始化：

[req.py:138-186](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L138-L186) 做了三件事：① 写身份与调度初值（`request_id`、`group_req_id`、各种长度清零、`finish_status = FinishStatus()`）；② 处理采样参数（若是 `SamplingParams` 直接用，否则用 dict 初始化）；③ 为变长数据分配专属 `ShmArray` 并写入 prompt。其中关键一行是：

```python
self.alloc_shm_numpy_len = self.input_len + self.sample_params.max_new_tokens + 1024  # + 1024 for safe
```

它为 prompt_ids / logprobs 预留了「输入 + 最大输出 + 1024 安全冗余」的长度，确保后续 decode 阶段写出的 token 不会越界。

prompt_ids 并不直接存在 `Req` 里，而是放在一个独立的 `ShmArray`，名字由服务名 + 槽位下标拼成，保证全局唯一：

[req.py:252-264](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L252-L264) 中 `create_prompt_ids_shm_array` 在创建端 `create_shm()`，`link_prompt_ids_shm_array` 在读取端 `link_shm()`。注意 `Req` 字段表里**并没有** `shm_prompt_ids`——它是 `init()` 里动态挂上去的 Python 属性，指向另一段共享内存。这种「主结构体放定长小字段、变长大数组单独开一块 shm」的设计，兼顾了结构体布局固定与变长数据的灵活。

最后看「能否释放」的判定，它是生命周期的终点闸门：

[req.py:295-308](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L295-L308) `can_release()` 要求**三个条件同时满足**：① `ref_count == 1`（只剩管理节点这一个引用）；② `can_released_mark == True`（末端模块已标记）；③ 对于正常结束的请求，还要 `out_tokens_queue.is_empty()`（输出队列已全部消费完）。被 abort 的请求则只要前两条满足即可立刻释放。这套条件保证了「没有进程还在用、输出也都吐干净了」才回收槽位。

> 补充：`Req` 有两个子类。`ChunkedPrefillReq`（[req.py:361-412](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L361-L412)）覆写了 `get_tuple_tokens` / `get_decode_need_tokens` 等「token 负载估算」方法，是 chunked prefill 调度（u2-l6）的核心；`TokenHealingReq`（[req.py:415-435](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L415-L435)）又继承它，通过 `post_init` 把末尾几个 token 移到 `prefix_token_ids` 实现前缀补全。运行时用哪个子类由 `ShmReqManager.get_req_class_type()` 决定（见 4.2）。

#### 4.1.4 代码实践：梳理 Req 的关键字段

这是一个**源码阅读型实践**，目标是把 4.1.1 那张「档案卡」从源码里亲手填一遍。

1. **实践目标**：把 `Req` 字段按「身份 / 调度 / KV 管理 / 输出与生命周期」四类归类，并标出每个字段的 C 类型与所在行号。
2. **操作步骤**：
   - 打开 [req.py:73-128](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L73-L128)。
   - 仿照下表，把字段填完（以下已给出示例行）：

     | 类别 | 字段 | 类型 | 行号 | 作用 |
     | --- | --- | --- | --- | --- |
     | 身份 | `request_id` | `c_int64` | L77 | 请求唯一 id |
     | 身份 | `index_in_shm_mem` | `c_int` | L74 | 共享内存槽位下标 |
     | 调度 | `input_len` | `c_int` | L79 | prompt 长度 |
     | 调度 | `is_paused` | `c_bool` | L91 | 是否因显存不足暂停 |
     | KV 管理 | `shm_cur_kv_len` | `c_int` | L82 | 当前占用 KV 长度 |
     | KV 管理 | `prompt_cache_len` | `c_int` | L88 | GPU 缓存命中长度 |
     | 输出/生命周期 | `can_released_mark` | `c_bool` | L105 | 末端标记可释放 |
     | 输出/生命周期 | `ref_count` | `c_int` | L75 | 引用计数 |
3. **需要观察的现象**：你会发现几乎所有「跨进程要读写的状态」都是 `c_int` / `c_bool` / 内嵌 `Structure`（如 `finish_status`、`out_tokens_queue`），而**变长大数组（prompt_ids、logprobs）不在字段表里**——它们通过 `init()` 动态挂为 `shm_*` 属性。
4. **预期结果**：得到一张完整的字段分类表，能清楚指出「调度靠哪几个字段、KV 管理靠哪几个字段」。
5. 待本地验证（若你想确认类型宽度）：在 Python 里 `import ctypes; print(ctypes.sizeof(...))` 需要 `set_env_start_args` 配套，较繁琐，建议先以阅读为主。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Req` 要用 `ctypes.Structure` 而不是普通 Python 类？
**答案**：因为 `Req` 要被多个进程通过共享内存直接共享。普通 Python 对象内存布局不固定、会被 GC 移动，无法被另一进程按地址访问；`ctypes.Structure` 有确定的字段顺序与字节对齐（`_pack_=4`），能通过 `from_buffer` 安全地铺在共享内存上，让多进程得到指向同一物理内存的视图。

**练习 2**：`shm_cur_output_len` 和 `candetoken_out_len` 都和「输出长度」相关，为什么要分两个字段？
**答案**：源码注释（L84-L87）说明：`cur_output_len` 会被多个进程并发访问，直接复用它传「detokenization 可解码长度」会有竞态风险；因此单独用 `candetoken_out_len` 由推理进程写给 detokenization 进程，隔离这条信息、避免多进程访问导致的数据不一致。

---

### 4.2 共享内存：ShmReqManager

#### 4.2.1 概念说明

`Req` 是「档案卡」，但卡片要放进「档案柜」里才能被管理。`ShmReqManager` 就是这个档案柜：它在启动时一次性开辟一大块共享内存，把它切成 `max_req_num` 个等大的 `Req` 槽位，并提供两套接口：

- **槽位分配接口**（`alloc_req_index` / `release_req_index`）：决定「第几个槽位给谁用」。只有管理请求生命周期的首节点（HttpServer）能调用。
- **对象引用接口**（`get_req_obj_by_index` / `put_back_req_obj`）：在槽位已分配后，各进程取出/归还 `Req` 对象引用，并维护 `ref_count`。

这套「两层管理」是理解 LightLLM 并发安全的关键：**分配是全局唯一的（一把管理锁），而引用计数是每个槽位一把锁**，从而把全局竞争降到最低。

#### 4.2.2 核心流程：槽位分配与引用计数

槽位数组本身的尺寸是确定的：

\[
\text{req\_shm\_byte\_size} = \text{sizeof}(Req) \times \text{max\_req\_num}
\]

其中 `max_req_num` 来自启动参数 `running_max_req_size`（见 u1-l4）。这块大内存创建一次后，被 `from_buffer` 解释成 `(Req) * max_req_num` 的数组，每个槽位的 `index_in_shm_mem` 就是数组下标。

空闲槽位用一个**存放在共享内存里的链表**（`ReqLinkedListManager`）管理：分配就是从链表头摘一个下标，释放就是挂回链表头。链表本身也在共享内存里，所以所有进程看到的是同一份空闲表。链表用一个 `next` 指针数组实现，0 号节点是头：

```
初始化:  head(0) -> 1 -> 2 -> 3 -> ... -> N-1 -> -1(空)
alloc(): 摘下 head 指向的第一个，返回 (下标-1)，head 前移
free(i): 把 i+1 挂到 head 后面
```

引用计数则更精细：`get_req_obj_by_index` 时 `ref_count += 1`，`put_back_req_obj` 时 `ref_count -= 1`，且每次都在「该槽位专属的锁」保护下进行。配合 4.1.3 的 `can_release()`（要求 `ref_count == 1`），就形成了「谁用谁计数、没人用了才回收」的安全闭环。

#### 4.2.3 源码精读

`ShmReqManager.__init__` 列出了它的全部初始化步骤：

[shm_req_manager.py:18-30](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_req_manager.py#L18-L30) 依次完成：选 Req 子类 → 算单结构体大小与总字节 → 建大块 shm → 把数组视图铺上去 → 建每槽位锁 → 建管理锁 → 建分配状态 shm。`get_req_class_type`（L32-L37）按 `token_healing_mode` 决定用 `TokenHealingReq` 还是 `ChunkedPrefillReq`。

最关键的一步是把共享内存「解释」成 Req 数组：

[shm_req_manager.py:58-63](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_req_manager.py#L58-L63) `init_to_req_objs` 用 `(self.req_class * self.max_req_num).from_buffer(self.reqs_shm.buf)` 直接在共享内存缓冲区上构造出 `max_req_num` 个 `Req`，并把每个槽位的 `ref_count` 清零、`index_in_shm_mem` 写成下标 `i`。此后任何进程对 `self.reqs[i]` 的读写都是对这块共享内存的原地操作。

槽位分配接口加着**全局管理锁**：

[shm_req_manager.py:91-112](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_req_manager.py#L91-L112) `alloc_req_index` 在 `manager_lock` 保护下从空闲链表摘一个下标、并把 `alloc_state_shm[idx]` 置 1；`release_req_index` 反向操作，当所有槽位都归还时打印 `"all shm req has been release ok"`。注意注释（L89-L90）：这两个接口**只有管理请求申请/释放的首节点**才能调用——即 HttpServer。

对象引用接口则用**每槽位锁**：

[shm_req_manager.py:123-141](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_req_manager.py#L123-L141) `get_req_obj_by_index` 在该槽位的 `AtomicLockItem` 保护下 `ref_count += 1`，`put_back_req_obj` 则 `-= 1`。它们各自维护一个**进程私有**的状态 `proc_private_get_state`（L86）来断言「本进程是否已经持有该对象」，防止同一进程对同一槽位重复 get。

空闲链表本身就在共享内存里，用 `next` 指针数组实现：

[shm_req_manager.py:150-191](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_req_manager.py#L150-L191) `ReqLinkedListManager` 用 0 号节点当链表头，初始化时把 `0..size-1` 串成链（`_initialize_values`，L161-L164）；`alloc`（L166-L172）摘头节点并返回「实际下标」（注意返回 `alloc_idx - 1`，因为 0 号是头）；`free`（L178-L182）把节点挂回头。这是一个经典的「共享内存里的对象池」实现。

支撑这一切的底层是两个小工具。`ShmArray`（[shm_array.py:9-34](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_array.py#L9-L34)）把 numpy 数组铺到共享内存：`create_shm` 在创建端用，`link_shm` 在读取端用 `force_mode="link"` 连接并校验大小。`AtomicShmLock`（[atomic_lock.py:10-49](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/atomic_lock.py#L10-L49)）用 4 字节共享内存 + `atomics` 库的 `cmpxchg_weak`（CAS）实现跨进程自旋锁。而 `create_or_link_shm`（[shm_utils.py:9-35](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/shm_utils.py#L9-L35)）统一封装了「先尝试 link、不存在再 create」的智能语义，并用 `/tmp/{name}.lock` 的 `FileLock` 防止创建竞争。

#### 4.2.4 代码实践：跑通分配/引用计数接口（可运行）

这是一个**可运行实践**，基于项目自带的单元测试。

1. **实践目标**：亲手调用 `ShmReqManager` 的分配/释放/引用计数接口，验证「分配置位、释放清零、get/put 改 ref_count」的行为。
2. **操作步骤**：
   - 直接运行现成测试（最快路径）：

     ```bash
     cd /path/to/lightllm
     python -m pytest unit_tests/server/core/objs/test_shm_req_manager.py -v
     ```
   - 阅读测试 [test_shm_req_manager.py:47-79](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/unit_tests/server/core/objs/test_shm_req_manager.py#L47-L79)，对照断言理解行为：
     - `test_alloc_req_index`：分配后 `alloc_state_shm.arr[index] == 1`。
     - `test_release_req_index`：释放后回到 `== 0`。
     - `test_get_req_obj_by_index`：get 后 `ref_count == 1`。
     - `test_put_back_req_obj`：put 后 `ref_count == 0`。
     - `test_alloc_req_index_no_available`：把 `max_req_num` 个槽位全占满后再 alloc，返回 `None`。
3. **需要观察的现象**：测试全部 PASSED；`test_performance_alloc_release` 会打印 100 次 alloc/release 的耗时（通常在毫秒级），可直观感受对象池开销。
4. **预期结果**：理解「`alloc_state_shm` 是全局分配标志位、`ref_count` 是每槽位引用计数」这两层状态各自由哪把锁保护。
5. 若环境缺依赖（如 `atomics`、`filelock`）导致无法运行，明确记为「待本地验证」，但可继续做下一条阅读实践。

#### 4.2.5 小练习与答案

**练习 1**：`alloc_req_index` 和 `get_req_obj_by_index` 为什么用不同的锁？
**答案**：`alloc_req_index` 操作的是**全局空闲链表**（所有进程共享的同一份下标表），必须用全局 `manager_lock` 串行化，否则两个进程会摘到同一个下标；`get_req_obj_by_index` 只改某个**特定槽位**的 `ref_count`，用该槽位专属的 `AtomicLockItem` 即可，互不阻塞，从而把全局竞争降到最低。

**练习 2**：`proc_private_get_state`（shm_req_manager.py L86）起什么作用？
**答案**：它是**每个进程私有**的数组（不在共享内存里），记录「本进程是否已持有该槽位」。`get_req_obj_by_index` 断言它为 0 才能 get、get 后置 1，防止同一进程对同一 `Req` 重复 get 导致 `ref_count` 重复增加、破坏配对关系。

---

### 4.3 进程间通信：ShmObjsIOBuffer

#### 4.3.1 概念说明

4.2 解决了「`Req` 本体放哪、怎么分配」。但 Router 和 ModelBackend 之间除了「共享 Req」，还需要传一些**轻量命令**：比如「这一批要 prefill 哪几个请求」「中止某个请求」「停止串命中了」。这些命令体量很小，但需要被**一个生产者写给多个消费者**（TP 模式下同一节点的多个 rank 都要读到）。

`ShmObjsIOBuffer` 就是为这类「单生产者、多消费者」轻量命令设计的共享内存管道。它用一段共享内存（默认 64MB）存放 pickle 后的命令字节，再用一个**原子计数器**做生产者-消费者同步。它的精髓在于：**真正的大对象（Req、prompt）从不进这个管道**，管道里走的只是 `(request_id, index_in_shm_mem, ...)` 这种几十字节的小元组。

#### 4.3.2 核心流程：write_obj / set_ready / sub_state 协议

这是一个经典的「就绪计数」协调协议，计数器存在共享内存的头 4 个字节（`int_view[0]`）。设节点内 rank 数为 `node_world_size = tp // nnodes`：

```
生产者 (Router):
  while not is_empty():        # 等所有消费者把上一轮读走
      sleep
  write_obj(cmds)              # pickle cmds 写入 buf[8:]
  set_ready()                  # int_view[0] = node_world_size

消费者 (每个 ModelBackend rank):
  if is_ready():               # int_view[0] == node_world_size
      obj = read_obj()         # 从 buf[8:] 反序列化
      sub_state()              # int_view[0] -= 1
  # 当所有 rank 都 sub_state 后，int_view[0] 归 0 → is_empty() 为真
```

关键点：`set_ready` 把计数器设为「消费者总数」，每个消费者读一次就 `sub_state` 减一，全部读完归零；生产者下一轮写入前用 `while not is_empty()` 自旋等待，保证上一轮命令被所有消费者消费完才覆写。这就实现了「一次写入、多 rank 各读一次、互不踩踏」。

#### 4.3.3 源码精读

`ShmObjsIOBuffer` 的全貌很短，先看它的初始化与缓冲区布局：

[shm_objs_io_buffer.py:14-26](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_objs_io_buffer.py#L14-L26) 在 `__init__` 里建一段默认 64MB 的共享内存（容量由 `LIGHTLLM_REQS_BUFFER_BYTE_SIZE` 环境变量控制，见 L9），并配一把 `AtomicShmLock`；`node_world_size = tp // nnodes` 决定了「就绪计数」的目标值。缓冲区前 8 字节是头部：`int_view[0]` 是就绪计数器，`int_view[1]` 是后续 pickle 字节的长度。

计数器的四个操作就是协议本身：

[shm_objs_io_buffer.py:22-41](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_objs_io_buffer.py#L22-L41) `set_ready` 在锁内断言当前为 0、写入 `node_world_size`；`sub_state` 断言 `> 0` 后减一；`is_empty` 判 `== 0`；`is_ready` 判 `== node_world_size`。全部在 `self.lock` 保护下，保证计数器自身的读改写原子。

真正「传对象」的两个方法用 pickle 序列化：

[shm_objs_io_buffer.py:43-52](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_objs_io_buffer.py#L43-L52) `write_obj` 把对象 `pickle.dumps` 成字节，先记长度到 `int_view[1]`，再把字节拷进 `shm.buf[8:]`；`read_obj` 反过来按长度读出并 `pickle.loads`。注意它只序列化**传入的对象本身**——至于传什么，完全取决于调用方。

**为什么这样能避免拷贝大对象？** 关键在调用方传的是什么。看 Router 生产端：

[router/manager.py:301-309](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L301-L309) `_add_batch` 先 `while not is_empty()` 等清空，再 `write_obj(reqs)`、`set_ready()`。而这里的 `reqs` 是：

```python
reqs = [r.to_router_rpc_obj() for r in batch.reqs]
```

`to_router_rpc_obj`（[req.py:286-293](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L286-L293)）只返回一个**小元组** `(request_id, index_in_shm_mem, multimodal_params, suggested_dp_index)`——绝不含 prompt_ids、权重或任何大块数据。所以 pickle 进缓冲区的只是「告知 ModelBackend 去哪个槽位读 Req」的几十字节指引，真正的大对象始终留在 4.2 的共享内存槽位里原地不动。`_aborted_reqs`、`_stop_str_matched_reqs`（[router/manager.py:322-336](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L322-L336)）同理，传的只是 `AbortedReqCmd(req_id)` 这类只带 id 的轻量命令。

消费端（ModelBackend）则是协议的另一半：

[base_backend.py:437-456](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L437-L456) `_read_reqs_buffer_and_init_reqs` 在判定 `is_ready()` 后（协调逻辑见 L386-L412 的 `_try_read_new_reqs_normal`，master rank 用 NCCL broadcast 把就绪标志广播给节点内所有 rank），`read_obj()` 取出命令列表、`sub_state()` 减计数，再按命令里的 `index_in_shm_mem` 去**自己的** `ShmReqManager` 视图里取 `Req`、据此初始化本进程的 `InferReq`。

把两端连起来，就看到了完整的设计闭环：

> 大对象（`Req` 槽位、prompt ShmArray）在启动时一次性铺在共享内存里常驻；Router 与 ModelBackend 之间每轮只通过 `ShmObjsIOBuffer` 传一批小元组「指针」，配合就绪计数协议保证多 rank 各读一次。**大对象从不被序列化、从不被拷贝**，这就是「对象放共享内存、线上只传索引」避免拷贝大对象的真正含义。

#### 4.3.4 代码实践：解释 write_obj/set_ready 如何避免拷贝大对象

这是一个**源码追踪型实践**，对应本讲指定的实践任务。

1. **实践目标**：用真实代码证明「`ShmObjsIOBuffer` 线上只传小元组，大对象留在共享内存」，并说清 `write_obj/set_ready` 在其中的角色。
2. **操作步骤**：
   - 第一步，看「写进缓冲区的到底是什么」。打开 [router/manager.py:301-309](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L301-L309)，确认传入 `write_obj` 的是 `[r.to_router_rpc_obj() ...]`。
   - 第二步，看 `to_router_rpc_obj` 的返回体量。打开 [req.py:286-293](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L286-L293)，确认它只返回 `(request_id, index_in_shm_mem, multimodal_params, suggested_dp_index)`。
   - 第三步，看「大对象在哪」。对比 [shm_req_manager.py:58-63](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_req_manager.py#L58-L63) 的 `Req` 数组与 [req.py:252-264](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L252-L264) 的 prompt ShmArray——它们才是「大对象」，且都常驻共享内存。
   - 第四步，看「协调协议」。打开 [shm_objs_io_buffer.py:22-47](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_objs_io_buffer.py#L22-L47)，把 `set_ready`/`sub_state`/`is_empty`/`write_obj` 四个方法和 [router/manager.py:304-307](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L304-L307) 的调用顺序对应起来。
3. **需要观察的现象**：你会清楚地看到，缓冲区里 pickle 的对象**只含 id 和 index**，没有任何 prompt token 或权重；而「等待上一轮消费完」靠的是 `while not is_empty()` + `set_ready()` 的计数握手。
4. **预期结果**：能用一句话回答本讲实践任务——「`write_obj` 只序列化小元组（含 `index_in_shm_mem`），`set_ready` 用就绪计数通知所有 rank 各读一次；真正的 `Req` 与 prompt 数组常驻共享内存、按 index 原地读写，因此从不被拷贝」。
5. 待本地验证（可选）：若想实测 pickle 体量，可在能 `set_env_start_args` 的环境里 `import pickle; print(len(pickle.dumps((1, 0, None, -1))))`，应只有几十字节。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Router 在 `write_obj` 之前要先 `while not is_empty(): await sleep`？
**答案**：因为 `ShmObjsIOBuffer` 是单缓冲区，`write_obj` 会覆写 `buf[8:]`。如果上一轮命令还没被所有 rank 消费完（计数器未归零就 `is_empty()` 为假），贸然覆写会让没读到的 rank 丢失数据。所以生产者必须等计数器归零、确认所有消费者都读完了，才写下一轮。

**练习 2**：在 TP=8、单机（nnodes=1）的场景下，`set_ready()` 会把计数器设成几？为什么？
**答案**：设成 `node_world_size = tp // nnodes = 8`。因为这 8 个 rank 同处一个节点、共享这段 `ShmObjsIOBuffer`，每个 rank 各读一次并 `sub_state` 减一，8 次之后归零，表示这一轮命令已被节点内所有 rank 消费完。

---

## 5. 综合实践：画出一次 prefill 命令的「索引之旅」

把本讲三个最小模块串起来，完成下面这个贯穿性任务。

**任务**：以「Router 把一个新 batch 通知给 ModelBackend」为场景，画出 `index_in_shm_mem` 这个整数的一次完整「旅行」，并标注每一段用的是哪条信道、改了哪个共享内存结构。

**建议步骤**：

1. 从 [router/manager.py:301-309](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L301-L309) 出发：Router 把每个 Req 调 `to_router_rpc_obj()` 得到含 `index_in_shm_mem` 的小元组。
2. 经 [shm_objs_io_buffer.py:43-47](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_objs_io_buffer.py#L43-L47) 的 `write_obj` pickle 进命令缓冲区，`set_ready` 置计数。
3. ModelBackend 经 [base_backend.py:438-439](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L438-L439) 的 `read_obj`/`sub_state` 取出元组，拿到 `index_in_shm_mem`。
4. ModelBackend 用这个 index 经 [shm_req_manager.py:123-130](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/shm_req_manager.py#L123-L130) 的 `get_req_obj_by_index` 在**自己的共享内存视图**里取出 `Req`，读 [req.py:79](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L79) 的 `input_len`、[req.py:280-281](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L280-L281) 的 `get_prompt_ids()`（背后是 prompt ShmArray）做推理，再把结果写回 [req.py:82-83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L82-L83) 的 `shm_cur_kv_len` / `shm_cur_output_len`。

**产出**：一张图或一段文字，清楚标出——

- 哪些步骤走「轻量消息」（`ShmObjsIOBuffer`，传小元组）；
- 哪些步骤走「共享内存常驻大对象」（`Req` 槽位、prompt ShmArray，按 index 原地读写）；
- `ref_count` 在 get/put 时如何变化，`can_released_mark` 何时被置位。

这张图就是你理解 LightLLM 整个多进程数据流的「钥匙」。

## 6. 本讲小结

- `Req` 是一个 `_pack_=4` 的 `ctypes.Structure`，字段分身份/调度/KV 管理/输出与生命周期四类；与调度最相关的是 `input_len`、`is_paused`、`finish_status`、`chunked_prefill_size`，与 KV 管理最相关的是 `shm_cur_kv_len`、`prompt_cache_len`、`cpu/disk_prompt_cache_len`。
- 变长大数据（prompt_ids、logprobs）不进 `Req` 字段表，而是由 `init()` 动态创建独立 `ShmArray`，名字带 `index_in_shm_mem` 保证全局唯一。
- `ShmReqManager` 把 `sizeof(Req) × max_req_num` 的大块共享内存用 `from_buffer` 解释成 Req 数组；分配用「全局管理锁 + 共享内存空闲链表」，引用计数用「每槽位原子锁」，形成两层并发管理。
- `ShmObjsIOBuffer` 是「单生产者多消费者」命令管道，用头 4 字节的就绪计数（`set_ready`/`sub_state`/`is_empty`）协调多 rank 各读一次，命令体靠 pickle 序列化。
- 「避免拷贝大对象」的真正含义：线上 `write_obj` 只 pickle `(request_id, index_in_shm_mem, ...)` 这种小元组，真正的 `Req` 与 prompt 数组常驻共享内存、按 index 原地读写——这正是「对象放共享内存、线上只传索引」的落地实现。
- `can_release()` 用「`ref_count==1` + `can_released_mark` + 输出队列已空」三条件，保证没有进程在用且输出吐净才回收槽位。

## 7. 下一步学习建议

本讲讲清了「请求对象是什么、放在共享内存哪里、进程间怎么传索引」。接下来：

- **u2-l4 Model Backend 推理后端与 RPC**：看 ModelBackend 拿到 `index_in_shm_mem` 后如何调用 `ShmReqManager.get_req_obj_by_index`、如何把 `Req` 包装成 `InferReq`、以及 rpyc 如何在 Router 与每 GPU 的 ModelRpcServer 之间承载调用。
- **u2-l5 Router 调度循环**：理解 Router 主循环里 `is_empty()` 判断、`schedule_new_batch`、`_add_batch` 的协同，看本讲的 `ShmObjsIOBuffer` 是如何被「每 30ms 一次」地驱动的。
- 若对 KV 管理字段（`shm_cur_kv_len` 等）如何参与调度感兴趣，可预习 **u4-l1 KV Cache 内存管理** 与 **u4-l3 Token 负载估算**。
