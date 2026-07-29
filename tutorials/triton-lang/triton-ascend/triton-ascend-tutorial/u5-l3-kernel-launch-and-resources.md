# 内核启动：rtKernelLaunch、workspace 与 sync_block_lock

## 1. 本讲目标

上一讲（u5-l2）我们看到 `make_launcher` 会**按签名**生成一段 C++ launcher 源码，编译成 `.so` 后供 Python 调用。本讲要回答的核心问题是：**当 Python 端的 `kernel[grid](...)` 真正触发时，这段生成的 C++ 代码究竟做了什么，才把一个 kernel 在 NPU 的 stream 上跑起来？**

读完本讲，你应当能够：

1. 说清 **launch_args 的内存布局**：编译器如何把一堆类型各异的参数按对齐规则打包成一段连续字节，让设备端 kernel 能按固定偏移读取。
2. 说清 **workspace 的分配**：它是给每个 block 用的工作内存，大小由谁推断、由谁分配。
3. 说清 **sync_block_lock 的分配与初始化**：它是一段跨核同步用的设备内存，如何申请、如何写入初值、如何释放。
4. 区分 **`rtKernelLaunch` 与 `rtKernelLaunchWithFlagV2`** 两条启动 API，说清后者额外携带了 `localMemorySize` 这条信息以及它为什么只在 950 SIMT 路径出现。

---

## 2. 前置知识

本讲假设你已经学完 u5-l1（设备发现与 `npu_utils.cpp`）和 u5-l2（launcher 代码生成与参数解析）。在此基础上补充三个概念：

- **stream（流）**：昇腾 NPU 的异步执行队列。host 端把任务（如启动一个 kernel）丢到 stream 上就返回，设备按顺序执行。因此「启动 kernel」本质是「往 stream 上提交一个启动任务」。
- **block**：一个 kernel 实例（对应 Triton 里的一个 program）。`gridX*gridY*gridZ` 就是本次启动的 block 总数（`blockNum`）。昇腾把 block 直接绑定到物理核（见 u2-l2），所以 block 数与核数强相关。
- **rt API**：CANN 运行时（runtime）提供的 C 接口，前缀 `rt`，如 `rtKernelLaunch`、`rtMemcpy`、`rtGetAiCoreCount`。launcher 这段 C++ 就是把这些 rt API 串起来完成启动。

还有一个关键直觉：**设备端 kernel 看不到 Python 对象，它只认一段连续的字节缓冲**。所以 launcher 的核心职责之一，就是把 Python 传进来的张量/标量「翻译」并「打包」成这段字节缓冲——这就是 `launch_args`。

> 提醒：本讲引用的 C++ 代码全部来自 `make_launcher` 在**运行期动态生成**的字符串（见 u5-l2），并非仓库里静态存在的 `.cpp` 文件。我们引用的是「生成这些 C++ 的 Python 模板」所在的行号。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [third_party/ascend/backend/driver.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py) | 本讲主战场。`make_launcher` 在此拼接出整段 C++ launcher，包含 `launch_args` 布局、workspace/sync_block_lock 分配、两条 `rtKernelLaunch` 调用。 |
| [third_party/ascend/backend/backend_register.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/backend_register.py) | `get_backend_func` 的策略表。`allocate_memory`/`allocate_sync_block_lock`/`async_launch` 等片段按 torch_npu/mindspore 分派注入到生成代码里。 |
| [third_party/ascend/backend/npu_utils.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/npu_utils.cpp) | 被 dlopen 加载的 `triton_allocate_workspace_legacy`/`triton_allocate_sync_block_lock`/`triton_async_launch` 的真实实现。 |
| [third_party/ascend/backend/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py) | `workspace_size`/`lock_num`/`lock_init_val`/`shared_mem_dynamic_size` 等元数据的来源。 |
| [third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp) | 往 IR 写入 `parallel_mode` 属性，决定了 `enable_simt`，进而决定走哪条启动 API。 |

---

## 4. 核心概念与源码讲解

### 4.1 launch_args：把参数打包成内核能读懂的字节流

#### 4.1.1 概念说明

设备端 kernel 是一段被 BiSheng 编译好的机器码，它启动时需要一批输入：若干个张量的设备地址、若干个标量（如 `n_elements`、`BLOCK_SIZE` 作为 `constexpr`）。kernel 的入口约定了这些输入在**一块连续内存**里的排列顺序与每个字段的大小/对齐。

`launch_args` 就是这块连续内存。launcher 要把 host 侧拿到的参数，按 kernel 期望的顺序、大小、对齐，逐个 `memcpy` 进去，再把「这块内存的指针 + 总字节数」交给 `rtKernelLaunch`。

为什么强调「对齐」？因为设备端会按类型对齐去读字段（如 `int64` 要 8 字节对齐），不对齐轻则性能下降，重则取错值。所以布局算法必须给每个字段补齐到其对齐要求。

#### 4.1.2 核心流程

`make_launcher` 生成了**两个**功能等价的启动函数，它们各自维护一份 `launch_args`，只是打包方式不同：

- `triton_launch_kernel`（`extern "C"`）：面向外部 C 调用方，参数以「指针数组 + 大小数组」传入，用 `std::vector<char>` **手工按偏移**填充。这段手工布局最直观，本讲用它讲清楚布局规则。
- `_launch`：Python 模块入口 `launch()` 实际调用的函数，参数已经是 typed 的 C 变量，直接用一个 `struct __attribute__((packed))` 让编译器算偏移。

两者产出的内存布局**完全一致**（kernel 不关心是哪条路径填的），字段顺序大致是：

```
[ffts_addr?] [syncBlockLock_ptr?] [workspace_addr_ptr?] [用户参数 arg0..argN] [gridX gridY gridZ] [DTData?]
```

带 `?` 的字段是条件性的：`ffts_addr` 仅支持 FFTS 的芯片出现；`syncBlockLock_ptr` 与 `workspace_addr_ptr` 在**非 `force_simt_only`** 路径才占据固定槽位；`DTData` 仅 device print 开启时出现。

布局算法用一个小工具函数做对齐——向上取整到对齐数的下一个倍数：

\[ \text{aligned}(o, a) = (o + a - 1)\ \&\ \sim(a - 1) \]

该式要求 `a` 是 2 的幂（本讲里 `a` 只取 1/4/8，均满足）。

#### 4.1.3 源码精读

**对齐工具函数**（生成代码中的内联函数）—— [_align_launch_offset, driver.py:L852-L854](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L852-L854)：把当前偏移 `offset` 向上对齐到 `alignment` 的倍数，正是上面公式的直接翻译。

**槽位预约**——在 `triton_launch_kernel` 中，用一个 lambda 逐个「预约」字段槽位，返回该字段在缓冲里的起始偏移，同时推进总偏移 `args_offset`：[driver.py:L954-L974](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L954-L974)。

关键片段（条件性字段已标注）：

```c
size_t args_offset = 0;
auto reserve_slot = [&](size_t size, size_t alignment) -> size_t {
  args_offset = _align_launch_offset(args_offset, alignment);
  size_t current_offset = args_offset;
  args_offset += size;
  return current_offset;
};
size_t ffts_offset            = reserve_slot(sizeof(void*), 8);  // 仅 FFTS 芯片
size_t sync_block_lock_offset = reserve_slot(sizeof(void*), 8);  // 仅非 force_simt_only
size_t workspace_offset       = reserve_slot(sizeof(void*), 8);  // 仅非 force_simt_only
size_t kernel_args_offset = args_offset;                          // 用户参数起点
for (int arg_idx = 0; arg_idx < num_args; ++arg_idx) {
  size_t alignment = launch_arg_sizes[arg_idx] >= 8 ? 8
                   : (launch_arg_sizes[arg_idx] >= 4 ? 4 : 1);    // 按大小选对齐
  args_offset = _align_launch_offset(args_offset, alignment);
  args_offset += launch_arg_sizes[arg_idx];
}
size_t grid_offset = reserve_slot(sizeof(int32_t), 4);            // gridX
reserve_slot(sizeof(int32_t), 4);                                 // gridY
reserve_slot(sizeof(int32_t), 4);                                 // gridZ
size_t dtdata_offset = reserve_slot(sizeof(void*), 8);            // 仅 device print
```

注意用户参数的对齐是**按字段大小自适应**的：≥8 字节对齐 8，≥4 字节对齐 4，否则对齐 1。这保证 `int64`/指针 8 字节对齐、`int32` 4 字节对齐，又不给小类型浪费空间。

**按偏移回填**——`reserve_slot` 只算了偏移，真正的写发生在分配完 `std::vector<char> launch_args(total_size, 0)` 之后：[driver.py:L976-L990](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L976-L990)。

```c
std::vector<char> launch_args(total_size, 0);
memcpy(launch_args.data() + ffts_offset, &ffts_addr, sizeof(void*));
memcpy(launch_args.data() + sync_block_lock_offset, &syncBlockLock_ptr, sizeof(void*));
memcpy(launch_args.data() + workspace_offset, &workspace_addr_ptr, sizeof(void*));
for (int arg_idx = 0; arg_idx < num_args; ++arg_idx) {
  kernel_arg_offset = _align_launch_offset(kernel_arg_offset, alignment);
  memcpy(launch_args.data() + kernel_arg_offset,
         copied_kernel_args[arg_idx].data(), launch_arg_sizes[arg_idx]);
  kernel_arg_offset += launch_arg_sizes[arg_idx];
}
memcpy(launch_args.data() + grid_offset, &gridX, sizeof(int32_t));
memcpy(launch_args.data() + grid_offset + sizeof(int32_t), &gridY, sizeof(int32_t));
memcpy(launch_args.data() + grid_offset + 2 * sizeof(int32_t), &gridZ, sizeof(int32_t));
```

**Python 路径的等价写法**——`_launch` 不手算偏移，而是让编译器算。它用一个带 `__attribute__((packed))` 的结构体，成员顺序与上面一致，每个成员再用 `__attribute__((aligned(N)))` 声明对齐：[driver.py:L1063-L1079](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L1063-L1079)。`packed` 告诉编译器「不要为了整体对齐在尾部加 padding」，配合每个成员各自的 `aligned`，得到的内存布局与手工版逐字节一致。

> 顺带交代 `triton_launch_kernel` 与 `_launch` 的分工：Python 模块只导出 `launch`，它解析完 Python 元组后调用 `_launch`（[driver.py:L1178](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L1178)）；而 `triton_launch_kernel` 是 `extern "C"` 导出符号（[driver.py:L856-L862](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L856-L862)），供宿主侧/外部 C 调用方以裸指针直接启动。两者复用同一套资源分配与启动逻辑。

#### 4.1.4 代码实践

**目标**：亲眼看到「生成出来的 launcher」里 `launch_args` 的真实布局。

1. 设置 `TRITON_DEBUG=1`（dump 出 launcher 源码，见 u5-l2），运行任意一个 tutorial kernel（如 `01-vector-add.py`）。
2. 在 dump 目录里找到形如 `launcher_cxx11abi1.cxx` 的文件并打开。
3. 定位 `_launch` 函数里的 `struct __attribute__((packed)) { ... } args = { ... };`。
4. **观察现象**：记录结构体里每个成员的顺序与类型。把 vector-add 的签名（两个指针 `*fp32` + 一个标量 `i32`）对照，确认你看到了 `syncBlockLock`、`workspace_addr`（若该路径含）、`arg0/arg1/arg2`、`gridX/gridY/gridZ` 这几类字段。
5. **预期结果**：成员顺序与本讲 4.1.2 的布局一致；指针类参数 `arg0/arg1` 被声明为 `void* __attribute__((aligned(8)))`，标量 `arg2` 为 `int32_t __attribute__((aligned(4)))`。
6. 若手头没有 NPU 设备，无法触发 dump，则标注「待本地验证」，改为纯阅读：在 [driver.py:L1067-L1068](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L1067-L1068) 处阅读生成结构体成员的模板逻辑即可。

#### 4.1.5 小练习与答案

**练习 1**：若用户参数里有一个 `i1`（布尔）类型，它在 `launch_args` 里会以多大对齐、占多少字节？
**答案**：`ty_to_cpp("i1") = "int32_t"`（[driver.py:L409](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L409)），所以大小 4 字节、对齐 4。

**练习 2**：为什么 `gridX/gridY/gridZ` 三个 `int32` 放在**所有用户参数之后**而不是最前面？
**答案**：因为设备端 kernel 的入口签名约定「先逐个接收用户参数，最后接收 grid」。grid 放尾部是和 kernel 侧 ABI 对齐的结果；放前面会导致偏移错位。

---

### 4.2 workspace 分配：为每个 block 准备一块工作内存

#### 4.2.1 概念说明

**workspace** 是一块设备侧（HBM）内存，供 kernel 运行时存放中间结果或临时数据——它的存在与否、大小，由 BiSheng 编译器在编译这个 kernel 时决定，host 端在启动前必须替它申请好，并把地址塞进 `launch_args`。

这块内存和 UB（Unified Buffer，片上 192/256 KB，见 u2-l3）不同：UB 是每核私有的片上高速缓存，而 workspace 是所有 block 共享的、位于 HBM 的「大块」工作区，容量远大于 UB。

#### 4.2.2 核心流程

1. 编译期：BiSheng 编译器把「每个 block 需要多少 workspace」编码进一个回调函数（`<kernel_name>_infer_workspace_shape_function`），随 `.o` 一起产出。
2. npubin 阶段：Triton 通过 ctypes 调用该回调，把字节数写进 `metadata["workspace_size"]`。
3. 启动期：launcher 计算 `totalWorkSpaceSize = workspace_size * blockNum4Workspace`（每个 block 一份），调用分配函数拿到设备地址 `workspace_addr_ptr`。
4. 把 `workspace_addr_ptr` 写入 `launch_args` 的 `workspace_offset` 槽位。

若 `workspace_size == 0`（kernel 不需要 workspace），整段分配代码都不会生成——这是 `if workspace_size > 0 else ''` 模板条件控制的。

#### 4.2.3 源码精读

**编译期推断 workspace 大小**——在 npubin 阶段，加载 BiSheng 产出的 `libkernel.so`，按符号名取回调并执行：[compiler.py:L752-L757](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L752-L757)。其中 `_infer_workspace_shape_function` 负责写入 `workspace_size`。回调解析的通用机制见 [__get_metadata_attr_by_callback, compiler.py:L343-L349](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L343-L349)：用 `kernel_name + postfix` 拼出符号名，`hasattr` 判断存在性后再 ctypes 调用。

**启动期分配**——在 `triton_launch_kernel` 中：[driver.py:L900-L910](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L900-L910)。

```c
void *workspace_addr_ptr = NULL;
uint32_t blockNum4Workspace = gridX * gridY * gridZ;
// （仅 workspace_size > 0 时生成下面这段）
uint64_t totalWorkSpaceSize = {workspace_size} * blockNum4Workspace;
{get_backend_func("allocate_memory", "totalWorkSpaceSize", "stream")}   // 注入分配代码
if (!workspace_addr_ptr) {
  fprintf(stderr, "Error: workspace allocation failed\n"); return;
}
```

注意 `blockNum4Workspace` 用的是**逻辑 grid 乘积**，**早于** auto-blockify 的 `std::min` 裁剪（裁剪发生在后面的 `blockNum`，见 4.4.3）。也就是说 workspace 按「逻辑上开了多少 block」来申请。

**分配函数的注入（torch_npu 分支）**——`get_backend_func("allocate_memory", ...)` 在 torch_npu 策略下注入：[backend_register.py:L304-L312](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/backend_register.py#L304-L312)。它先 `init_npu_utils()` 确保 dlopen 拿到函数指针，再调用 `g_allocate_workspace_legacy(size)`。

**真正分配的实现**——`triton_allocate_workspace_legacy` 用 PyTorch 的 NPU 缓存分配器开一块 byte 张量，返回其存储首地址：[npu_utils.cpp:L358-L364](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/npu_utils.cpp#L358-L364)。

```cpp
extern "C" void *triton_allocate_workspace_legacy(uint64_t size) {
  return const_cast<void *>(
      at::empty(size, at::TensorOptions().device(at::kPrivateUse1).dtype(at::kByte))
          .storage().data());
}
```

`at::kPrivateUse1` 正是 torch_npu 注册的 `npu` 设备。走 PyTorch 缓存分配器的好处是：workspace 的生命周期与显存回收交由 torch_npu 统一管理，不漏不爆。

#### 4.2.4 代码实践

**目标**：理解 workspace 大小是「每 block 一份」，并确认它在 vector-add 这类简单 kernel 下通常为 0。

1. 阅读 [compiler.py:L754-L755](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L754-L755)，确认 `workspace_size` 来自回调。
2. 阅读 [driver.py:L430-L431](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L430-L431)：`make_launcher` 把 `metadata.workspace_size` 读进 `workspace_size`，缺省为 `-1`（`<0` 视同 0，不生成分配代码）。
3. **预期结果**：对 `01-vector-add.py` 这类无中间大张量的 kernel，BiSheng 通常不要求 workspace，dump 出的 `.cxx` 里**不会**出现 `totalWorkSpaceSize` 那段代码。含大中间缓冲的 kernel（如某些 reduce/matmul）才会出现。
4. 若能在设备上运行，把 `BLOCK_SIZE` 调大重新编译同一 kernel，对比 dump 中 workspace 是否变化；无法运行则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `totalWorkSpaceSize = workspace_size * blockNum4Workspace`，而不是直接用 `workspace_size`？
**答案**：`workspace_size` 是**每个 block** 所需字节数；本次启动有 `blockNum4Workspace` 个 block，所以总量要乘以 block 数。

**练习 2**：workspace 分配失败时（`workspace_addr_ptr` 为空），launcher 会怎样？
**答案**：打印 `Error: workspace allocation failed` 并 `return`，**不启动 kernel**（[driver.py:L553, L908-L909](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L908-L909)）。这是「快速失败」避免带空指针上设备。

---

### 4.3 sync_block_lock 分配：跨核同步锁的申请与初始化

#### 4.3.1 概念说明

**sync_block_lock** 是一块用于**跨核同步**的设备内存。当多个 block 之间需要协调（典型场景：离散 store 的读-改-写需要跨核互斥，见 u4-3；或用户显式写了 `tl.sync_block_*`，见 u7-l4）时，各核会读写这段共享内存里的「锁变量」来表达临界区与同步点。

它和 workspace 的区别在于**用途与初始化**：workspace 是数据缓冲，初值无所谓；sync_block_lock 是同步原语，必须在启动前**写入确定的初值**（通常为 0 表示「空闲」），否则锁状态不可预测，直接导致死锁或数据竞争。

#### 4.3.2 核心流程

1. 编译期：BiSheng 回报**锁的数量** `lock_num` 与**初值** `lock_init_val`（同样以回调形式）。
2. 启动期：
   - 计算 `syncBlockLockSize = lock_num * sizeof(int64_t)`（每个锁 8 字节）。
   - 调用分配函数拿到设备地址 `syncBlockLock_ptr` 与一个 retain 句柄 `syncBlockLock_handle`。
   - 用 `std::shared_ptr<void>` + `release_npu_tensor_handle` 自定义删除器，保证启动后自动释放底层张量。
   - 在 host 侧构造初值数组 `lockInitData`（`lock_num` 个 `int64` 全等于 `lock_init_value`），用 `rtMemcpy(..., RT_MEMCPY_HOST_TO_DEVICE)` 把它拷到设备。
   - 把 `syncBlockLock_ptr` 写入 `launch_args` 的 `sync_block_lock_offset` 槽位。

只在 `lock_num > 0` 时才生成这一整段。`lock_init_value` 的读取兼容了新旧两种字段名 `lock_init_value` / `lock_init_val`（[driver.py:L432-L435](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L432-L435)）。

#### 4.3.3 源码精读

**编译期推断**——`lock_num` 与 `lock_init_val` 来自两个回调：[compiler.py:L756-L757](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L756-L757)。

**启动期分配 + 初始化**（`triton_launch_kernel` 内）：[driver.py:L932-L951](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L932-L951)。

```c
void *syncBlockLock_ptr = NULL;
void *syncBlockLock_handle = NULL;
// （仅 lock_num > 0 时生成）
uint64_t syncBlockLockSize = {lock_num} * sizeof(int64_t);
{get_backend_func("allocate_sync_block_lock", "syncBlockLockSize", "stream")}
std::shared_ptr<void> syncBlockLock_handle_guard(syncBlockLock_handle, release_npu_tensor_handle);
if (!syncBlockLock_ptr) { ... }                      // 失败处理
std::vector<int64_t> lockInitData({lock_num}, {lock_init_value});
ret = rtMemcpy(syncBlockLock_ptr, syncBlockLockSize,
               reinterpret_cast<void *>(lockInitData.data()), syncBlockLockSize,
               RT_MEMCPY_HOST_TO_DEVICE);            // 把初值 H2D 拷到设备
if (ret != RT_ERROR_NONE) { return ...; }
```

**分配函数注入（torch_npu 分支）**——注意它比 workspace 多接收一个 `stream` 与一个 `&syncBlockLock_handle`：[backend_register.py:L321-L329](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/backend_register.py#L321-L329)。

**真实实现**——`triton_allocate_sync_block_lock` 调 torch_npu 的 `allocate_workspace`（一个**感知 stream** 的工作区分配器），并通过 `retainTensor` 把张量句柄交还给 host 以便后续释放：[npu_utils.cpp:L366-L375](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/npu_utils.cpp#L366-L375)。释放走 [triton_release_retained_tensor, npu_utils.cpp:L377-L380](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/npu_utils.cpp#L377-L380)，正是上面 `shared_ptr` 删除器调用的函数。

> 为什么 workspace 用「无 stream 的 legacy 分配」而 sync_block_lock 用「带 stream 的分配」？因为锁与一次具体的 stream 提交强相关（释放时机要和该 stream 上的执行对齐），故走 torch_npu 的 stream-aware 工作区；workspace 则复用更简单的全局缓存分配器。两者最终都落到 torch_npu 的显存管理之下。

#### 4.3.4 代码实践

**目标**：触发一个真正需要 sync_block_lock 的 kernel，观察其分配代码。

1. 查阅 [compiler.py:L368-L396](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L368-L396)：当 IR 含 `sync_block_lock` 标记时，`has_auto_blockify_blacklist_op` 被置真（auto-blockify 关闭）。这印证「锁与 auto-blockify 互斥」。
2. 阅读 u4-l3 提到的「离散 store 的读-改-写」场景，理解锁从何而来。
3. 在能 dump 的环境下，找一个含非连续 mask store 的 kernel（会触发 `sync_block_lock`），打开 `.cxx`，定位 `syncBlockLockSize = ... * sizeof(int64_t)` 与 `rtMemcpy(... RT_MEMCPY_HOST_TO_DEVICE)` 两行。
4. **预期结果**：能看到 `lockInitData` 是一个全 `lock_init_value`（常为 0）的 vector，并被 H2D 拷到 `syncBlockLock_ptr`。
5. 无设备则标注「待本地验证」，改为阅读 [_launch 中的对应模板 driver.py:L1042-L1061](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L1042-L1061)。

#### 4.3.5 小练习与答案

**练习 1**：sync_block_lock 为什么必须用 `rtMemcpy` 写初值，而不能像 workspace 那样「分配完就不管」？
**答案**：锁是同步原语，初值不确定会让核间看到「未初始化的锁状态」，导致错误的同步语义（误以为已上锁/已解锁）。所以必须先把确定初值（如 0）刷到设备。

**练习 2**：`std::shared_ptr<void> syncBlockLock_handle_guard(syncBlockLock_handle, release_npu_tensor_handle);` 这行的 RAII 作用是什么？
**答案**：当 `syncBlockLock_handle_guard` 出作用域（本次启动结束）时，自动调用 `release_npu_tensor_handle` 释放底层 retain 的张量，避免显存泄漏；同时保证释放发生在启动提交之后。

---

### 4.4 rtKernelLaunch vs rtKernelLaunchWithFlagV2：两条启动 API

#### 4.4.1 概念说明

参数打包好、workspace 与锁都就位后，最后一步是调用 CANN runtime 的启动 API，把 kernel 提交到 stream。Triton-Ascend 有**两条**启动 API：

- **`rtKernelLaunch(func, blockNum, args, argsSize, smDesc, stream)`**：标准启动，参数以「裸指针 + 字节数」传入。
- **`rtKernelLaunchWithFlagV2(func, blockNum, argsInfo, smDesc, stream, flag, cfgInfo)`**：增强启动，参数包在 `rtArgsEx_t` 里，并额外带一个 `rtTaskCfgInfo_t`（关键是 `localMemorySize` 字段）和一个 `flag`。

两者的选择由一个条件决定：`compile_on_910_95 and enable_simt`。也就是说，**只有在 950（Ascend 910_95 / 950）硬件、且 kernel 处于 SIMT 相关模式时**，才用增强版。

#### 4.4.2 核心流程

`enable_simt` 的判定是本节关键：

```python
enable_simt = ("simt" in parallel_mode) or metadata.force_simt_only
```

其中 `parallel_mode` **不是** `NPUOptions` 里的那个默认值，而是 triton-to-linalg pass **写进 IR 的函数属性**，再被 `_parse_linalg_metadata` 正则抠出来（[compiler.py:L399](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L399)）。pass 里的逻辑是：默认 `"simd"`；一旦发现 SIMT 算子（如 `IndirectLoadOp`/`IndirectStoreOp`，见 u6-l2），就改成 `"mix_simd_simt"`：[TritonToLinalgPass.cpp:L479-L484](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L479-L484)。

于是 `"simt" in "mix_simd_simt"` 为真 → `enable_simt=True`。仓库里有一条注释把整条链路写得非常清楚：「`parallel_mode -> "mix_simd_simt" -> enable_simt -> launch reserves localMemorySize`」：[TritonToLinalgPass.cpp:L978-L980](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/lib/TritonToLinalg/TritonToLinalgPass.cpp#L978-L980)。

当 `compile_on_910_95 and enable_simt` 为真，启动调用切换为 `rtKernelLaunchWithFlagV2`，并通过 `cfgInfo.localMemorySize` 告诉 runtime：**本任务的每个 block 需要多少「local memory」（AI 核上类似 GPU shared memory 的动态局部存储）**。

#### 4.4.3 源码精读

`make_launcher` 先准备一段「普通启动」的 C++ 字符串，随后用 `if compile_on_910_95 and enable_simt:` **整体覆盖**为「增强启动」：[driver.py:L809-L820](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L809-L820)（这是 `triton_launch_kernel` 路径，参数来自 `std::vector<char> launch_args`）。

```c
// 默认（普通启动）
ret = rtKernelLaunch(func, blockNum,
                     static_cast<void*>(launch_args.data()), launch_args.size(),
                     NULL, stream);

// 950 + SIMT（增强启动）
rtArgsEx_t argsInfo = {};
argsInfo.args = static_cast<void*>(launch_args.data());
argsInfo.argsSize = launch_args.size();
rtTaskCfgInfo_t cfgInfo = {};
cfgInfo.localMemorySize = {metadata.shared_mem_dynamic_size};   // 关键额外信息
ret = rtKernelLaunchWithFlagV2(func, blockNum, &argsInfo, NULL, stream, 0, &cfgInfo);
```

`_launch` 路径有一份等价的覆盖逻辑（参数来自 packed struct `args`）：[driver.py:L821-L832](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L821-L832)。

**`localMemorySize` 的取值**——来自 `metadata.shared_mem_dynamic_size`，由 `NPUOptions.__post_init__` 决定：`force_simt_only` 时默认 122880（120 KB），否则 221184（216 KB）：[compiler.py:L1122-L1126](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1122-L1126)。

**blockNum 的裁剪**——在调用前，`blockNum` 可能被 auto-blockify 裁剪到物理核数：[driver.py:L922](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L922)（`blockNum = std::min(blockNum, num_physical_blocks)`，仅在 auto-blockify 开启且无黑名单算子时）。`num_physical_blocks` 按 `mix_mode` 选 Vector 核数或 AI 核数：[driver.py:L547](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L547)。这是 u2-l2「强物理核心绑定」在启动期的落地。

**同步 vs 异步提交**——整段启动逻辑被包进一个 lambda。默认 `TRITON_ENABLE_TASKQUEUE=true` 时，该 lambda 被异步派发（torch_npu 经 `triton_async_launch` → `OpCommand` 提交，见 [backend_register.py:L351-L358](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/backend_register.py#L351-L358) 与 [npu_utils.cpp:L382-L386](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/npu_utils.cpp#L382-L386)）；关闭 taskqueue 时则 lambda 直接执行，并在结尾 `rtStreamSynchronize(stream)` 同步等待（[driver.py:L997-L998](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L997-L998)）。

#### 4.4.4 代码实践（本讲核心实践任务）

**目标**：对比普通启动与 950 SIMT 启动两段 C++ 代码，说清 `rtKernelLaunchWithFlagV2` 额外携带了哪些信息。

1. 打开 [driver.py:L809-L820](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L809-L820)，把两段代码并排抄下来。
2. 逐参数对照两者签名，列出**差异**。
3. **需要观察与说明的差异**：
   - 入参打包方式：普通版用裸 `void* + size`；增强版包成 `rtArgsEx_t argsInfo`（结构体里同样是 `args` 指针 + `argsSize`，但封装成结构体便于扩展）。
   - 额外的 `rtTaskCfgInfo_t cfgInfo`：这是普通版**完全没有**的信息。其中 `cfgInfo.localMemorySize = shared_mem_dynamic_size` 显式告诉 runtime 每个 block 需要多少动态 local memory——这是 SIMT 执行模型在 950 上必须配置的资源。
   - 额外的 `flag` 参数（此处传 `0`）：预留的启动标志位，普通版没有这个槽位。
4. **预期结论**：`rtKernelLaunchWithFlagV2` 相对 `rtKernelLaunch` 额外携带的核心信息是 **per-block 的 `localMemorySize`**（外加一个 flag 槽）。它之所以只在 950 SIMT 路径出现，是因为 SIMT kernel 需要显式申请/声明核上动态局部存储，而 SIMD 路径不需要。
5. 想在设备上验证：分别在 950 和非 950 机器 dump 同一 kernel 的 `.cxx`，定位 `rtKernelLaunch` 那一行，确认前者为 `WithFlagV2`、后者为普通版。无法验证则标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：一个纯向量、无非连续访存的简单 kernel（如 vector-add），在 950 上会走哪条启动 API？为什么？
**答案**：走普通 `rtKernelLaunch`。因为它的 IR 不含 SIMT 算子，`parallel_mode` 保持 `"simd"`，`"simt" in "simd"` 为假，`force_simt_only` 也为假，故 `enable_simt=False`。

**练习 2**：把 `localMemorySize` 设得比实际需要小，会发生什么？
**答案**：runtime 给每个 block 分配的核上局部存储不足，kernel 运行时越界访问 local memory，通常表现为运行时错误或结果错误。所以该值由编译期 `shared_mem_dynamic_size` 给定，不能随意改小。

**练习 3**：为什么判定条件里要有 `compile_on_910_95`，而不是所有芯片都用增强版？
**答案**：`rtKernelLaunchWithFlagV2` 与 `rtTaskCfgInfo_t` 的 `localMemorySize` 语义是 950 代硬件（及其 SIMT 执行模型）才需要的；早期芯片（910B/910D）的 SIMD 模型用普通 `rtKernelLaunch` 即可，且其 runtime 不一定支持该增强语义。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「dump 一份 launcher，逐段讲清启动全流程」的源码阅读实践：

1. 开启 `TRITON_DEBUG=1` 运行一个含 `tl.dot` 且带非连续访存的 kernel（能同时触发 workspace、sync_block_lock 与 SIMT 路径最为理想），拿到 `launcher_cxx11abi1.cxx`。
2. 在 `_launch` 函数体内，按执行顺序定位并标注以下五段：
   - **参数解析**：`PyArg_ParseTuple` 与 `getPointer`（u5-l2）。
   - **workspace 分配**：`totalWorkSpaceSize = ... * blockNum4Workspace` 与 `allocate_memory` 注入段（4.2）。
   - **sync_block_lock 分配与初始化**：`syncBlockLockSize`、`allocate_sync_block_lock`、`rtMemcpy(... RT_MEMCPY_HOST_TO_DEVICE)`（4.3）。
   - **args 打包**：`struct __attribute__((packed)) { ... } args = { ... };`（4.1）。
   - **最终启动**：`rtKernelLaunch` 或 `rtKernelLaunchWithFlagV2`（4.4）。
3. 画一张时序图（文字即可）：host 侧从「收到 Python 调用」到「把启动任务提交到 stream」之间，上述五步的先后与依赖（例如 args 打包必须在 workspace/锁分配之后，因为要把它们的指针写进 args）。
4. 最后回答：你这台机器（若是 950）走的是哪条 `rtKernelLaunch`？`localMemorySize` 被设成了多少？依据是 `shared_mem_dynamic_size` 的哪个分支？

> 若无设备，第 1 步无法真实 dump，可改为：直接阅读 [driver.py:L1004-L1089](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L1004-L1089) 的 `_launch` 模板，在脑中把模板变量替换成一个具体签名，完成同样的标注与时序图。

---

## 6. 本讲小结

- **launch_args** 是一段按对齐规则手工（或用 packed struct）打包的连续字节，字段顺序为「ffts? / syncBlockLock? / workspace? / 用户参数 / gridX,gridY,gridZ / DTData?」；对齐用 `(o+a-1) & ~(a-1)` 向上取整。
- **workspace** 是每 block 一份的 HBM 工作内存，大小由 BiSheng 回调 `_infer_workspace_shape_function` 推断，`workspace_size>0` 才生成分配代码，经 torch_npu 缓存分配器申请。
- **sync_block_lock** 是跨核同步用的设备内存，`lock_num*8` 字节，必须用 `rtMemcpy(H2D)` 写入 `lock_init_value` 初值，并用 `shared_ptr`+`release_npu_tensor_handle` 自动释放。
- 两条启动 API 的分水岭是 `compile_on_910_95 and enable_simt`，其中 `enable_simt` 取决于 IR 里的 `parallel_mode` 是否含 `"simt"`（典型值 `"mix_simd_simt"`，由 triton-to-linalg pass 在发现 SIMT 算子时写入）。
- **`rtKernelLaunchWithFlagV2`** 相比普通版额外携带 `rtTaskCfgInfo_t.localMemorySize`（取自 `shared_mem_dynamic_size`）与一个 flag 槽，用于向 950 SIMT 执行模型声明 per-block 动态局部存储。
- 启动任务默认经 `TRITON_ENABLE_TASKQUEUE` 异步派发到 stream（torch_npu 走 `OpCommand`），关闭时才 `rtStreamSynchronize` 同步等待。

---

## 7. 下一步学习建议

- 本讲只讲了「启动」这一动作。如果想看启动前的**编译分流**如何决定 `force_simt_only`/`parallel_mode`，请进 u6-l1（compile_mode 三种模式）与 u6-l2（离散访存 SIMT 模板）。
- 想了解 sync_block_lock 在 IR 侧的来源，回顾 u4-l3（DiscreteMaskAccessConversion）和 u7-l4（同步原语与 compile_hint）。
- 后续 u9（自动调优）会反复触发「编译 + 启动」，届时你会再次看到本讲的 launcher 缓存与启动流程，建议把本讲作为运行时侧的参照底座。
- 若想动手扩展运行时，可尝试在 dump 出的 `.cxx` 上手动改一个参数（如把 `localMemorySize` 调大），重新编译该 `.so` 并观察行为差异——这是进入 u10（调试与二次开发）前很好的热身。
