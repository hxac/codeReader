# GPU 张量抽象与 CPU 后端

> 本讲属于「GPU 后端」单元（u5）的第一篇，是进入 Metal/CUDA/ROCm 三个具体后端之前的公共地基。
> 依赖前置讲义：u3-l2（权重绑定）、u4-l1（DeepSeek V4 单层数据流）。

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `ds4_gpu.h` 提出的 **tensor-resident（张量常驻设备）执行模型** 是什么，以及它为什么不同于「每次算子都上传权重 / 下载结果」的朴素模型。
- 列举并解释设备张量的生命周期原语：`alloc / view / write / read / copy / free`，以及命令缓冲的生命周期：`begin_commands / flush_commands / end_commands / synchronize`。
- 解释 **model map** 这层零拷贝映射：为什么模型权重通过「注册一次 mmap 基址」而不是「每次拷贝上传」喂给 GPU。
- 解释 **Q4 expert table 预加载** 解决了 PRO 模型 384 专家 MoE 的什么问题。
- 说清 **CPU 参考后端** 与 `DS4_NO_GPU` 编译开关的角色：它不是 `ds4_gpu_tensor` 的又一个实现，而是一条完全独立的、用宿主 `float *` 数组的可移植参考路径。

## 2. 前置知识

本讲默认你已经掌握：

- **mmap 权重加载**（u3-l1）：几十到几百 GB 的 GGUF 被 `mmap` 映射进进程虚拟地址空间，`model->map` 是这块映射的宿主基址，`model->size` 是大小，权重字节始终留在映射区不拷贝。
- **权重绑定**（u3-l2）：GGUF 里按字符串命名的张量被填进 `ds4_weights` / `ds4_layer_weights` 两张语义指针表，每个张量记有 `abs_offset`（在 mmap 区里的绝对偏移）和 `bytes`。
- **单层数据流**（u4-l1）：一层 transformer 包含 MLA 注意力 + router 选 6 个 routed expert + 1 个 shared expert，前向时激活、KV 状态、中间 buffer 在多个算子间流动。
- **后端枚举与编译开关**（u1-l4、u2-l1）：`ds4_backend` 有 `METAL / CUDA / CPU` 三值（ROCm 复用 `CUDA` 这一位）；`-DDS4_NO_GPU` 与 `-DDS4_ROCM_BUILD` 是两个编译期开关。

几个本讲要用到的术语，先用一句话点透：

- **device / 设备**：指 GPU（Apple Metal 设备、NVIDIA CUDA 设备、AMD ROCm 设备）。相对地，**host / 宿主**指 CPU 与普通进程内存。
- **零拷贝（zero-copy）**：让设备内核直接读写宿主映射的字节，而不是先把字节从宿主 `memcpy` 到一块设备显存。
- **命令缓冲（command buffer）**：把一组 GPU 算子「录制」成一个批，一次性提交给设备异步执行。Metal、CUDA、ROCm 都有这个概念。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [ds4_gpu.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h) | 所有 GPU 后端的**公共 C 接口**：张量原语、命令缓冲、model map、expert table、上百个算子内核声明。 | 张量与命令原语、model map、expert table 的函数签名与文档注释。 |
| [ds4.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c) | 引擎核心：模型加载、权重绑定、推理图调度、**CPU 参考生成路径**、引擎打开/关闭。 | 后端枚举判断、生成分发、`generate_raw_swa_cpu`、引擎打开时的 GPU 初始化与 expert table 预加载。 |
| [ds4_metal.m](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m) | Metal 后端实现（ObjC）。 | 用作「图后端如何实现 `ds4_gpu.h`」的具体参照。 |
| [ds4_cuda.cu](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cuda.cu) | CUDA 后端实现。 | 张量结构体定义、CUDA 的 model map（`cudaHostRegisterMapped`）。 |
| [ds4.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h) | 引擎公共窄头。 | `ds4_backend` 枚举。 |
| [Makefile](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile) | 构建脚本。 | `cpu` 目标与 `-DDS4_NO_GPU`、`CPU_CORE_OBJS` 不含后端对象。 |

一句话定位：`ds4_gpu.h` 是「合同」，`ds4_metal.m` / `ds4_cuda.cu` / `ds4_rocm.cu` 是三个「履约方」，`ds4.c` 里的 CPU 路径则是「不签这份合同的第三方」——它走自己的参考实现。

## 4. 核心概念与源码讲解

### 4.1 张量与命令原语

#### 4.1.1 概念说明

设想最朴素的 GPU 推理方式：每算一个算子，就把输入从宿主内存 `memcpy` 到设备，让设备算完，再把输出 `memcpy` 回宿主。对于 DeepSeek V4 这种一层就有几十个算子、一次 prefill 要跑 43 层的模型，这种「上传—算—下载」的往返会把大部分时间花在总线搬运上，完全不可行。

`ds4_gpu.h` 顶部那段注释明确给出了 ds4 的替代方案——**tensor-resident（张量常驻设备）**：

> activations, KV state, and scratch buffers stay device-owned across the whole prefill/decode command sequence.
> （激活、KV 状态、scratch 缓冲在整个 prefill/decode 命令序列期间一直归设备所有。）

也就是说：

- **激活、KV 缓存、中间 scratch** 这些推理过程中要被反复读写的数据，一旦在设备上分配，就**全程留在设备上**，算子之间直接用设备指针交接，不回宿主。
- 宿主只在两个时机碰这些数据：开头写入少量输入（如 token id、prompt 嵌入），结尾读回少量输出（如 argmax 选出的 token id、logits）。
- 这些常驻数据被抽象成一个不透明类型 `ds4_gpu_tensor`，对外只暴露一组**原语（primitive）函数**操作它。

`ds4_gpu_tensor` 在头里只是前向声明（不透明指针）：

```c
typedef struct ds4_gpu_tensor ds4_gpu_tensor;
```

来源：[ds4_gpu.h:L11-L20](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L11-L20) —— 中文说明：注释确立 tensor-resident 模型；`ds4_gpu_tensor` 对外是不透明类型，真实结构体藏在各后端 `.m` / `.cu` 文件里。

这样设计的好处：推理图调度代码（写在 `ds4.c` 里，纯 C）只依赖这组 C 函数签名，不依赖 Metal 的 `id<MTLBuffer>` 或 CUDA 的 `void*`，从而同一份调度代码能驱动三个后端。

#### 4.1.2 核心流程

一个典型的 prefill/decode 命令序列长这样（伪代码）：

```
ds4_gpu_begin_commands()                 # 开始录制一个命令缓冲

# —— 在此期间，所有算子读写的张量都常驻设备 ——
t_x   = ds4_gpu_tensor_alloc(bytes)      # 分配激活（设备拥有）
ds4_gpu_tensor_write(t_x, 0, host_buf,n) # 只在开头把少量输入写进去
ds4_gpu_matmul_q8_0_tensor(t_y, model_map, ..., t_x, n_tok)   # 设备内算
ds4_gpu_rms_norm_weight_tensor(t_z, t_y, model_map, ...)
... 几十个算子，张量之间直接设备内交接 ...
ds4_gpu_argmax_tensor(t_out, t_logits, n_vocab)               # 设备内 argmax
ds4_gpu_tensor_read(t_out, 0, &token_id, 4)  # 只在结尾读回 4 字节结果

ds4_gpu_end_commands()                   # 录制结束，提交并等待
```

可以把它想成「**录制 → 提交**」两段式：`begin_commands` 与 `end_commands` 之间的一切都先被录进命令缓冲，`end_commands`（或中途的 `flush_commands`）才把整批交给设备异步执行。这与 Metal 的 `MTLCommandBuffer` / CUDA 的 stream 提交模型一一对应。

为什么要批量录制？因为 GPU 内核启动有固定开销，把一整段 prefill 的几十个算子录成一个命令缓冲、一次性提交，能让设备流水线化执行，宿主也不必每个算子都等一次。

#### 4.1.3 源码精读

**(a) 张量生命周期原语。** 头文件第 25–39 行集中声明了操作 `ds4_gpu_tensor` 的全部原语：

```c
ds4_gpu_tensor *ds4_gpu_tensor_alloc(uint64_t bytes);
ds4_gpu_tensor *ds4_gpu_tensor_alloc_managed(uint64_t bytes);
ds4_gpu_tensor *ds4_gpu_tensor_view(const ds4_gpu_tensor *base, uint64_t offset, uint64_t bytes);
void ds4_gpu_tensor_free(ds4_gpu_tensor *tensor);
uint64_t ds4_gpu_tensor_bytes(const ds4_gpu_tensor *tensor);
void *ds4_gpu_tensor_contents(ds4_gpu_tensor *tensor);
int ds4_gpu_tensor_fill_f32(ds4_gpu_tensor *tensor, float value, uint64_t count);
int ds4_gpu_tensor_write(ds4_gpu_tensor *tensor, uint64_t offset, const void *data, uint64_t bytes);
int ds4_gpu_tensor_read(const ds4_gpu_tensor *tensor, uint64_t offset, void *data, uint64_t bytes);
int ds4_gpu_tensor_copy(...);
int ds4_gpu_tensor_copy_f32_to_f16(...);
```

来源：[ds4_gpu.h:L25-L39](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L25-L39) —— 中文说明：张量原语全家桶。按职责可分四组：

| 组 | 函数 | 作用 |
| --- | --- | --- |
| 分配/释放 | `alloc`、`alloc_managed`、`view`、`free` | 在设备上开一块字节缓冲；`view` 是零拷贝子视图（共享底层 buffer，只改 offset/bytes）；`alloc_managed` 在 Metal 上等价于普通 `alloc`。 |
| 查询/寻址 | `bytes`、`contents` | 取字节数；`contents` 拿到宿主可写指针（Metal 的 shared buffer 特性，宿主与设备共享同一块物理内存）。 |
| 宿主↔设备 | `write`、`read`、`fill_f32` | 在张量与宿主 `void*` 之间搬字节；`fill_f32` 用一个标量填满。 |
| 设备↔设备 | `copy`、`copy_f32_to_f16` | 设备内拷贝；后者附带 f32→f16 精度转换。 |

`view` 是个关键省内存手段：很多算子需要的是同一块大 buffer 的不同切片（比如某一层的 KV 行），`view` 不复制字节，只新建一个 `(buffer, offset, bytes)` 三元组。

**(b) 这些原语在后端里到底是什么。** 以 Metal 为例，`ds4_gpu_tensor` 真身是 ObjC 类 `DS4MetalTensor`，包了一个 Metal buffer：

```objc
@interface DS4MetalTensor : NSObject
@property(nonatomic, strong) id<MTLBuffer> buffer;
@property(nonatomic, assign) uint64_t offset;
@property(nonatomic, assign) uint64_t bytes;
@property(nonatomic, assign) uint8_t owner;   // 1=拥有 buffer，free 时释放；0=view，不释放
@end
```

来源：[ds4_metal.m:L466-L470](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L466-L470) —— 中文说明：Metal 张量的真实结构，`owner` 标记区分「自有 buffer」与「view 借来的 buffer」。

CUDA 后端则更朴素，就是一个宿主指针（CUDA 用统一/映射内存时宿主与设备同址）：

```c
struct ds4_gpu_tensor {
    void *ptr;
    uint64_t bytes;
    int owner;
};
```

来源：[ds4_cuda.cu:L42-L46](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cuda.cu#L42-L46) —— 中文说明：CUDA 张量结构体，`ptr` 在映射内存模型下宿主与设备共用。

`alloc` 落到 Metal 上就是开一块 **shared** 模式的 buffer（宿主与设备共享物理页）：

```c
ds4_gpu_tensor *ds4_gpu_tensor_alloc(uint64_t bytes) {
    ...
    DS4MetalTensor *tensor = [DS4MetalTensor new];
    tensor.buffer = [g_device newBufferWithLength:(NSUInteger)bytes
                                          options:MTLResourceStorageModeShared];
    tensor.offset = 0;
    tensor.bytes = bytes;
    tensor.owner = 1;
    ...
}
```

来源：[ds4_metal.m:L6200-L6237](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L6200-L6237) —— 中文说明：Metal 张量分配，用 `StorageModeShared` 让宿主与设备共享内存，所以 `write` / `read` 其实就是普通 `memcpy`。

正因如此，Metal 的 `write` / `read` 实现极简——直接对 buffer 的 `contents` 做 `memcpy`：

```c
int ds4_gpu_tensor_write(ds4_gpu_tensor *tensor, uint64_t offset, const void *data, uint64_t bytes) {
    ...
    if (bytes != 0) {
        memcpy((uint8_t *)[obj.buffer contents] + obj.offset + offset, data, (size_t)bytes);
    }
    return 1;
}
```

来源：[ds4_metal.m:L6326-L6344](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L6326-L6344) —— 中文说明：因为是 shared buffer，宿主写入即对设备可见，无需显式上传。

而设备↔设备的 `copy` 则不一样，它必须录进命令缓冲、用一个 **blit encoder**（块拷贝编码器）来搬字节：

```c
int ds4_gpu_tensor_copy(ds4_gpu_tensor *dst, uint64_t dst_offset,
                        const ds4_gpu_tensor *src, uint64_t src_offset, uint64_t bytes) {
    ...
    if (!g_batch_cb) return 0;            // 必须在 begin_commands 之后
    ds4_gpu_close_batch_encoder();
    id<MTLBlitCommandEncoder> blit = [g_batch_cb blitCommandEncoder];
    [blit copyFromBuffer:s.buffer sourceOffset:... toBuffer:d.buffer destinationOffset:... size:...];
    [blit endEncoding];
    return 1;
}
```

来源：[ds4_metal.m:L6346-L6368](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L6346-L6368) —— 中文说明：设备内拷贝走 blit 命令编码器，且必须在已打开的命令缓冲里录制——注意那个 `if (!g_batch_cb) return 0;`，它说明 `copy` 不是立即执行的。

**(c) 命令缓冲生命周期原语。** 头文件第 41–55 行：

```c
int ds4_gpu_begin_commands(void);
int ds4_gpu_flush_commands(void);
int ds4_gpu_signal_selected_readback_ready(uint64_t *event_value);
int ds4_gpu_commit_and_wait_selected_readback(uint64_t event_value, const char *label);
int ds4_gpu_wait_selected_readback_ready(uint64_t event_value, const char *label);
int ds4_gpu_end_commands(void);
int ds4_gpu_synchronize(void);
```

来源：[ds4_gpu.h:L41-L55](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L41-L55) —— 中文说明：命令缓冲的生命周期函数。`begin_commands` 开一个命令缓冲，`flush_commands` 在不结束整体录制的前提下提前提交一批（让设备先干起来，宿主继续录下一批），`end_commands` 收尾并等待，`synchronize` 是单纯的设备栅栏。

`flush` 与 `end` 的区别很关键，看 Metal 实现就明白——`flush` 提交当前命令缓冲后**立刻新开一个**继续录：

```c
int ds4_gpu_flush_commands(void) {
    ...
    ds4_gpu_close_batch_encoder();
    id<MTLCommandBuffer> cb = g_batch_cb;
    g_batch_cb = nil;
    [cb commit];                          // 提交当前批，设备开始异步执行
    [g_pending_cbs addObject:cb];
    g_batch_cb = ds4_gpu_new_command_buffer();   // 立刻新开一个，宿主继续录
    ...
}
```

来源：[ds4_metal.m:L6422-L6441](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L6422-L6441) —— 中文说明：`flush` 的「提交一批、再开一批」实现，用于让设备干活与宿主录制重叠，隐藏延迟。

`begin_commands` 则更简单，就是新建一个命令缓冲：

```c
int ds4_gpu_begin_commands(void) {
    if (!g_initialized && !ds4_gpu_init()) return 0;
    if (g_batch_cb) return 0;             // 已经在录了，不允许重入
    g_batch_cb = ds4_gpu_new_command_buffer();
    ...
    return g_batch_cb != nil;
}
```

来源：[ds4_metal.m:L6414-L6420](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L6414-L6420) —— 中文说明：`begin_commands` 不允许重入（已有命令缓冲就直接返回 0），保证同一时刻只有一个活跃录制。

#### 4.1.4 代码实践

**实践目标**：把 `ds4_gpu.h` 的张量与命令原语梳理成一张「生命周期图」，并验证「设备↔设备拷贝必须在命令缓冲内」这件事。

**操作步骤（源码阅读型）**：

1. 打开 [ds4_gpu.h:L25-L55](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L25-L55)，把第 25–39 行的张量原语抄进四张表（分配/释放、查询/寻址、宿主↔设备、设备↔设备）。
2. 在 `ds4.c` 里搜索 `ds4_gpu_begin_commands` 的调用点（本讲已知有数十处，例如 [ds4.c:L17201](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L17201) 与 [ds4.c:L19903](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L19903) 附近），观察它们总是成对出现：`bool ok = ds4_gpu_begin_commands() != 0;` 开头，结尾 `if (ok) ok = ds4_gpu_end_commands() != 0;`。
3. 对比 [ds4_metal.m:L6326-L6344](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L6326-L6344)（`write`/`read` 直接 memcpy）与 [ds4_metal.m:L6346-L6368](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L6346-L6368)（`copy` 走 blit encoder 且要求 `g_batch_cb` 非空）。

**需要观察的现象**：

- `write` / `read` / `fill_f32` 这类宿主↔设备原语**不检查** `g_batch_cb`，因为 shared buffer 下宿主写入立即可见。
- `copy` / `copy_f32_to_f16` 这类设备↔设备原语**必须**在 `begin_commands` 之后调用，否则 `if (!g_batch_cb) return 0;` 直接失败。

**预期结果**：你能画出这样一条生命周期——`alloc`（任何时候）→ `begin_commands` → 一堆算子 + `write/read/copy` → `flush`（可选，提前提交）→ 更多算子 → `end_commands`（提交并等待）→ `free`。

#### 4.1.5 小练习与答案

**练习 1**：`ds4_gpu_tensor_view` 产生的子张量，调用 `ds4_gpu_tensor_free` 会释放底层 Metal buffer 吗？

> **答案**：不会。`view` 创建的对象 `owner=0`（见 [ds4_metal.m:L462](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L462) 与 [ds4_metal.m:L6249-L6273](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L6249-L6273)），`free` 只清理 view 对象本身。底层 buffer 的释放责任仍在当初 `alloc` 出来的那个 `owner=1` 的父张量上。

**练习 2**：为什么 `ds4_gpu_tensor_copy` 要「录进命令缓冲」，而 `ds4_gpu_tensor_write` 不用？

> **答案**：`write` 走的是宿主与设备共享的 `StorageModeShared` 内存，宿主 `memcpy` 即对设备可见，是宿主侧的即时操作。`copy` 是「设备内」搬字节（源和目的都在设备 buffer 里），必须由设备的 blit 引擎执行，所以要先录进命令缓冲、随 `commit` 一起交给设备。

**练习 3**：`begin_commands` 如果在已经有一个活跃命令缓冲时再次调用会怎样？

> **答案**：直接返回 0（失败），见 [ds4_metal.m:L6416](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L6416) 的 `if (g_batch_cb) return 0;`。这是防重入保护，保证同一时刻只有一个录制中的命令缓冲。

---

### 4.2 model map 与 expert table

#### 4.2.1 概念说明

张量常驻解决了「激活 / KV / scratch」的常驻问题，但还有一类数据：**模型权重**。权重有几 GB 到几百 GB，不可能也不应该用 `ds4_gpu_tensor_alloc` + `ds4_gpu_tensor_write` 整体上传到设备——那既慢又占双份内存（mmap 一份 + 设备一份）。

ds4 的解法是 **model map**：把 u3-l1 里 mmap 出来的那块宿主模型字节，**注册一次**给设备后端，让它建立一个**设备可见的零拷贝映射**。此后所有算子读取权重时，只需告诉它「权重在这块映射里的偏移 `weight_offset`」，内核就能按 `(model_map + offset)` 直接寻址，**绝不拷贝权重字节**。

注意 `ds4_gpu.h` 里几乎所有 matmul 类算子的签名都长这样——既不收「设备权重张量」，也不收裸指针，而是收「map 基址 + map 大小 + 权重偏移」三元组：

```c
int ds4_gpu_matmul_q8_0_tensor(
        ds4_gpu_tensor       *out,
        const void             *model_map,     // mmap 宿主基址
        uint64_t                model_size,    // mmap 大小
        uint64_t                weight_offset, // 该权重在 map 里的绝对偏移
        uint64_t                in_dim, uint64_t out_dim,
        const ds4_gpu_tensor *x, uint64_t n_tok);
```

来源：[ds4_gpu.h:L224-L232](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L224-L232) —— 中文说明：典型算子签名，权重以 `model_map + weight_offset` 形式零拷贝寻址，而不是作为设备张量传入。这正是「model map」在算子层的体现。

「map 基址 + 偏移」还有一个附带好处：**模型权重天然只读**。算子拿到的只是「在某偏移处读一段字节」的能力，没有写权重的接口，避免误改 mmap 里的权重。

第二个机制是 **Q4 expert table 预加载**。PRO 模型每层有 384 个 routed expert（Q4_K 量化），router 每个 token 选 6 个。如果每次 MoE 算子都现去 map 里按 expert id 拼地址、反量化、gather，开销很大。ds4 在引擎打开时，对 PRO Q4 模型**预构建一张设备端的 expert 查找表**（含每个 expert 的 gate/up/down 地址），之后 MoE 内核按 expert id 直接查表即可。这张表由 `ds4_gpu_preload_q4_expert_tables` 一次性建好。

#### 4.2.2 核心流程

model map 的注册流程（引擎打开时一次性完成）：

```
model_open()                                # u3-l1：mmap 加载 GGUF，得到 model->map / model->size
  ↓
ds4_gpu_init()                              # 初始化设备/命令队列/各种 cache
  ↓
ds4_gpu_set_model_map(model->map, model->size)   # 或 _range / _spans 变体
  ↓                                  # 设备后端建立零拷贝映射：
                                     #   Metal = 把 mmap 包成 shared MTLBuffer 视图
                                     #   CUDA  = cudaHostRegisterMapped 注册成设备可寻址
  ↓
此后每个算子：ds4_gpu_*_tensor(..., model_map, model_size, weight_offset, ...)
```

SSD 流式场景下，注册的不是整个模型，而是「按需的若干 span」——通过 `ds4_gpu_set_model_map_spans` 注册多个不连续区段（哪几段 expert 要常驻）。

expert table 预加载流程：

```
引擎打开，检测到是 PRO Q4 模型（384 expert、Q4_K）
  ↓
对每一层 il：
    gate/up/down 三个专家张量的 abs_offset 与 per-expert 字节数已知
    ↓
    ds4_gpu_preload_q4_expert_tables(model->map, model->size,
                                     gate_offset, up_offset, down_offset,
                                     gate_expert_bytes, down_expert_bytes,
                                     n_total_expert=384)
  ↓
设备端为该层建好 Q4 expert 查找表，后续 MoE 内核按 id 查表
```

#### 4.2.3 源码精读

**(a) model map 注册接口。** 头文件第 57–66 行是一组同义函数，区别只在「注册整块 / 一段 / 多段」：

```c
int ds4_gpu_set_model_map(const void *model_map, uint64_t model_size);
int ds4_gpu_set_model_fd(int fd);
int ds4_gpu_set_model_fd_for_map(int fd, const void *model_map);
int ds4_gpu_set_model_map_range(const void *model_map, uint64_t model_size,
                                uint64_t map_offset, uint64_t map_size, uint64_t max_tensor_bytes);
int ds4_gpu_set_model_map_spans(const void *model_map, uint64_t model_size,
                                const uint64_t *offsets, const uint64_t *sizes,
                                uint32_t count, uint64_t max_tensor_bytes);
int ds4_gpu_cache_model_range(...);
int ds4_gpu_cache_q8_f16_range(...);
```

来源：[ds4_gpu.h:L57-L66](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L57-L66) —— 中文说明：model map 注册族。`set_model_map` 注册整块；`_range` 注册一段 `[map_offset, map_offset+map_size)`；`_spans` 注册多段（SSD 流式按需常驻用）；`cache_*` 把某段权重转格式后缓存到设备。

**(b) Metal 如何把 mmap 变成零拷贝 buffer。** 看 `set_model_map_range` 的实现：它先查缓存（是否已为同一段建过映射），没有就调 `ds4_gpu_map_model_views` 建立 shared buffer 视图，并记下 `g_model_map_ptr` 等状态：

```c
int ds4_gpu_set_model_map_range(const void *model_map, uint64_t model_size,
                                uint64_t map_offset, uint64_t map_size, uint64_t max_tensor_bytes) {
    ...
    if (g_model_map_ptr == model_map && ... 同段 ...) return 1;   // 命中缓存，直接复用
    ...
    ds4_gpu_model_residency_clear();
    if (!ds4_gpu_map_model_views(model_map, model_size, map_offset, map_size, max_tensor_bytes)) {
        ...; return 0;
    }
    g_model_map_ptr = model_map;
    g_model_mapped_offset = map_offset;
    g_model_mapped_size = map_size;
    ...
}
```

来源：[ds4_metal.m:L7148-L7189](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L7148-L7189) —— 中文说明：Metal 的 model map 注册——把 mmap 的一段包成 shared buffer 视图，且对相同区段的重复注册做缓存命中，避免重建。

**(c) CUDA 的零拷贝：host-mapped 内存。** CUDA 后端用 `cudaHostRegisterMapped` 把宿主 mmap 区注册成「设备可寻址」：

```c
unsigned int flags = cudaHostRegisterMapped | cudaHostRegisterReadOnly;
...
cudaError_t err = cudaHostRegister((void *)model_map, (size_t)model_size, flags);
```

来源：[ds4_cuda.cu:L2578-L2582](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cuda.cu#L2578-L2582) —— 中文说明：CUDA 用 `cudaHostRegisterMapped` 让宿主 mmap 区被设备直接寻址，随后内核通过 `cudaHostGetDevicePointer` 拿到的设备地址按 `weight_offset` 读取权重。这就是 CUDA 侧的「model map」。

两种后端手法不同（Metal 包 buffer / CUDA 注册映射），但**对外契约相同**：注册一次 mmap 基址，之后算子按偏移零拷贝读权重。

**(d) ds4.c 在哪里调用 set_model_map。** SSD 流式路径在 `metal_graph_install_model_spans` 里把若干 span 注册给设备：

```c
const bool ok = ds4_gpu_set_model_map_spans(model->map,
                                            model->size,
                                            offsets, sizes,
                                            spans->len,
                                            spans->max_tensor_bytes) != 0;
```

来源：[ds4.c:L11311-L11316](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L11311-L11316) —— 中文说明：SSD 流式时按 span 把模型区段注册给 Metal，`model->map` 就是 u3-l1 mmap 出来的宿主基址。

**(e) expert table 预加载接口与触发点。** 头文件第 67–71 行：

```c
int ds4_gpu_pro_q4_expert_table_auto_available(void);
int ds4_gpu_preload_q4_expert_tables(const void *model_map, uint64_t model_size,
                                     uint64_t gate_offset, uint64_t up_offset, uint64_t down_offset,
                                     uint64_t gate_expert_bytes, uint64_t down_expert_bytes,
                                     uint32_t n_total_expert);
```

来源：[ds4_gpu.h:L67-L71](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L67-L71) —— 中文说明：`pro_q4_expert_table_auto_available` 探测当前设备是否支持自动建表；`preload_q4_expert_tables` 为一层建 Q4 expert 查找表。

`ds4.c` 在引擎打开后，对每一层 PRO Q4 调用它——注意触发前的多重门控：必须是 Q4_K、384 expert、6 used，且设备支持或用户显式 opt-in：

```c
if (layer->ffn_gate_exps->type != DS4_TENSOR_Q4_K ||
    layer->ffn_up_exps->type   != DS4_TENSOR_Q4_K ||
    layer->ffn_down_exps->type != DS4_TENSOR_Q4_K ||
    DS4_N_EXPERT != 384 || DS4_N_EXPERT_USED != 6) {
    continue;                       // 不是 PRO Q4 配置，跳过
}
...
if (!ds4_gpu_preload_q4_expert_tables(e->model.map, e->model.size,
                                      layer->ffn_gate_exps->abs_offset,
                                      layer->ffn_up_exps->abs_offset,
                                      layer->ffn_down_exps->abs_offset,
                                      gate_expert_bytes, down_expert_bytes,
                                      DS4_N_EXPERT)) { ... }
```

来源：[ds4.c:L25501-L25531](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25501-L25531) —— 中文说明：引擎打开时逐层预加载 Q4 expert 表，门控严格（仅 PRO Q4 / 384 expert / 6 used），传入的是三个专家张量在 mmap 里的 `abs_offset`，再次印证「权重靠偏移寻址」。

#### 4.2.4 代码实践

**实践目标**：验证「权重通过 model map 一次性零拷贝注册，而非每次算子上传」这一论断。

**操作步骤（源码阅读型）**：

1. 在 [ds4_gpu.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h) 里数一下：有多少个算子声明同时带 `const void *model_map, uint64_t model_size, uint64_t weight_offset` 三个参数？（提示：从 [L224](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L224) 的 `matmul_q8_0_tensor` 数到 [L1006](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L1006) 的 `matmul_q8_0_hc_expand_tensor`，不下二十处。）
2. 对比这些算子的参数里**有没有**任何一个「设备权重张量」类型的参数（即 `const ds4_gpu_tensor *weights`）？预期：**没有**。权重永远以偏移形式寻址。
3. 在 `ds4.c` 里搜 `ds4_gpu_set_model_map` 与 `preload_q4_expert_tables`，确认它们都出现在**引擎打开阶段**（`ds4_engine_open` 内），而不是每次生成时。

**需要观察的现象**：算子签名里频繁出现 `model_map + weight_offset`，但从不出现「上传好的权重张量」；注册 model map 的调用集中在引擎初始化期。

**预期结果**：你能用一句话回答任务里的问题——**因为权重体积巨大且全程只读，注册一次零拷贝映射后按偏移寻址，能省掉每次算子都把权重搬上设备的巨额总线开销与双份内存**；而激活/KV 体积小且要被反复读写，所以走 `ds4_gpu_tensor` 常驻。

#### 4.2.5 小练习与答案

**练习 1**：算子签名里 `weight_offset` 是相对谁的偏移？

> **答案**：相对 `model_map`（即 u3-l1 mmap 出来的 GGUF 宿主基址）的**字节偏移**。它就是张量绑定阶段算出来的 `tensor->abs_offset`（见 u3-l2）。

**练习 2**：`ds4_gpu_set_model_map_range` 在 Metal 上检测到「同一段已经映射过」时会怎么做？

> **答案**：直接返回 1（命中缓存），不重建映射。见 [ds4_metal.m:L7155-L7161](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L7155-L7161)。这避免了在 SSD 流式反复注册同一 span 时反复建 buffer。

**练习 3**：为什么 Q4 expert table 预加载的门控要检查 `DS4_N_EXPERT != 384 || DS4_N_EXPERT_USED != 6`？

> **答案**：因为预加载的是为 **PRO 模型**（384 专家、Top-6）专门设计的 Q4 查找表内核。Flash 模型的专家数 / Top-k 不同，对应的 MoE 内核与表布局也不同，所以遇到非 PRO 配置直接 `continue` 跳过（见 [ds4.c:L25501-L25507](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25501-L25507)）。

---

### 4.3 CPU 参考后端

#### 4.3.1 概念说明

很多项目的「CPU 后端」是 GPU 后端的一个降级实现——同样的张量抽象、同样的算子接口，只是落在 CPU 上。**ds4 不是这样。**

ds4 的 CPU 路径是一条**完全独立的参考实现**，它根本不使用 `ds4_gpu_tensor`、不使用命令缓冲、不实现 `ds4_gpu.h` 里的任何一个算子声明。它用最朴素的宿主 `float *` 数组、`ds4_kv_cache` 结构体、以及一组 `layer_attn_*` / `embed_*` / `prefill_layer_major_cpu` 这样的宿主 C 函数，把 DeepSeek V4 的同一套数学（u4-l1 讲过的 MLA + MoE + HC）用可移植的标量/向量化 C 写一遍。

这条路径有三个作用：

1. **可移植与无 GPU 机器**：任何能编 C 的机器都能跑，不依赖 Metal / CUDA / ROCm 工具链。
2. **正确性参考**：它是「金标准」。GPU 后端的 logits 漂移要用它对照（见 u11-l3 的 golden 向量）。
3. **可读性**：标量 C 代码比 Metal Shader 易读得多，是理解模型数学的最佳入口。

控制这条路径是否存在的是编译开关 `-DDS4_NO_GPU`。它的影响是**编译期**的、彻底的：

- `ds4.c` 以 `-DDS4_NO_GPU` 重编为 `ds4_cpu.o`；
- 该编译单元里所有 `ds4_gpu_*` 调用都被 `#ifndef DS4_NO_GPU ... #else ... #endif` 切除，替换成「返回错误 / 不支持」的桩；
- **链接期不包含任何后端对象**（没有 `ds4_metal.o` / `ds4_cuda.o` / `ds4_rocm.o`），所以那些 `ds4_gpu_*` 符号根本不存在于最终二进制里。

#### 4.3.2 核心流程

后端选择是「编译期 + 运行期」两层叠加：

```
编译期：make cpu → -DDS4_NO_GPU → ds4_cpu.o（无后端对象）
                       ↓
运行期：default_backend() 见 DS4_NO_GPU → 返回 DS4_BACKEND_CPU
                       ↓
运行期：ds4_backend_uses_graph(CPU) == false
                       ↓
生成分发：不走 generate_metal_graph_raw_swa，而走 generate_raw_swa_cpu
                       ↓
generate_raw_swa_cpu：
    kv_cache_init(&cache, ctx_size, 0)              # 宿主 KV 缓存
    prefill_layer_major_cpu(logits, model, weights, &cache, prompt, ...)  # 一次性填 KV
    while (i < n_predict):
        token = sample_argmax(logits, DS4_N_VOCAB)  # 宿主采样
        decode_eval_one_cpu(...)                    # 自回归前进一格
```

#### 4.3.3 源码精读

**(a) 后端枚举与图后端判定。** 三种后端在公共头里枚举：

```c
typedef enum {
    DS4_BACKEND_METAL,
    DS4_BACKEND_CUDA,   // ROCm 在编译期复用这一位
    DS4_BACKEND_CPU,
} ds4_backend;
```

来源：[ds4.h:L19-L23](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L19-L23) —— 中文说明：三种后端枚举，注意 CPU 是独立一值。

引擎核心用一个判定函数区分「图后端（用 `ds4_gpu_*`）」与「CPU 参考后端」：

```c
static bool ds4_backend_uses_graph(ds4_backend backend) {
    return backend == DS4_BACKEND_METAL || backend == DS4_BACKEND_CUDA;
}
```

来源：[ds4.c:L75-L77](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L75-L77) —— 中文说明：只有 Metal/CUDA 走图后端（`ds4_gpu_tensor` + 命令缓冲）；CPU 不走，它有自己的参考路径。这是理解「CPU 不是又一个 GPU 后端」的关键一行。

**(b) 默认后端由编译期宏决定。** CLI 的 `default_backend()` 直接反映编译期状态：

```c
static ds4_backend default_backend(void) {
#ifdef DS4_NO_GPU
    return DS4_BACKEND_CPU;
#elif defined(__APPLE__)
    return DS4_BACKEND_METAL;
#else
    return DS4_BACKEND_CUDA;
#endif
}
```

来源：[ds4_cli.c:L182-L190](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L182-L190) —— 中文说明：`DS4_NO_GPU` 编译期开关直接让默认后端变成 CPU，把编译期与运行期选择串联起来。`parse_backend`（[ds4_cli.c:L165-L180](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L165-L180)）则负责 `--backend` 参数到枚举的映射。

**(c) 生成分发：图后端 vs CPU。** 引擎的生成入口按 `ds4_backend_uses_graph` 二选一：

```c
if (ds4_backend_uses_graph(e->backend)) {
#ifndef DS4_NO_GPU
        if (!e->metal_ready) { ...; return 1; }
        return generate_metal_graph_raw_swa(model, vocab, weights, prompt, ...);
#else
        fprintf(stderr, "ds4: ... this build has no graph backend support\n");
        return 1;
#endif
}
return generate_raw_swa_cpu(model, vocab, weights, prompt, ...);
```

来源：[ds4.c:L25158-L25193](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25158-L25193) —— 中文说明：生成的总分发。CPU 后端（`uses_graph==false`）直接落到 `generate_raw_swa_cpu`，与图后端走两条完全不同的代码路径。注意 `#ifdef DS4_NO_GPU` 分支：CPU 构建里即使误请求图后端，也只是报错退出，因为图后端代码已被切除。

**(d) CPU 参考生成的真身。** `generate_raw_swa_cpu` 全程用宿主 `float *` 与宿主 KV，看不到任何 `ds4_gpu_*`：

```c
static int generate_raw_swa_cpu(...) {
    fprintf(stderr, "ds4: using CPU generation with layer-major prefill\n");

    ds4_kv_cache cache;
    kv_cache_init(&cache, (uint32_t)ctx_size, 0);
    ds4_cpu_decode_scratch decode_scratch;
    cpu_decode_scratch_init(&decode_scratch, (uint32_t)ctx_size);

    float *logits = xmalloc((size_t)DS4_N_VOCAB * sizeof(logits[0]));
    ...
    prefill_layer_major_cpu(logits, model, weights, &cache, prompt, ...);
    ...
    for (int i = 0; i < n_predict && pos < ctx_size; i++) {
        int token = sample_argmax(logits, DS4_N_VOCAB);   // 宿主 argmax
        if (token == vocab->eos_id) break;
        if (emit) emit(emit_ud, token);
        ...
    }
}
```

来源：[ds4.c:L22802-L22876](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22802-L22876) —— 中文说明：CPU 参考生成的骨架——`ds4_kv_cache` + 宿主 `float *logits` + `prefill_layer_major_cpu` + `sample_argmax`，标准的 prefill→decode 自回归循环，但全部是宿主 C，没有任何设备张量或命令缓冲。

**(e) 编译期切除的证据。** 当 `DS4_NO_GPU` 定义时，依赖图后端的功能会变成「不支持」桩。例如分布式层快照保存：

```c
int ds4_session_save_layer_payload(...) {
    ...
    if (ds4_session_is_cpu(s)) {
        payload_set_err(err, errlen, "distributed layer payloads require the graph backend");
        return 1;
    }
#ifdef DS4_NO_GPU
    payload_set_err(err, errlen, "graph backend support is not compiled in");
    return 1;
#else
    ... 真正的图后端序列化 ...
#endif
}
```

来源：[ds4.c:L23676-L23679](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L23676-L23679) —— 中文说明：`DS4_NO_GPU` 下图后端相关代码被 `#else` 桩替换，运行期直接返回「未编译」。这类切除点在 `ds4.c` 里有数十处（见 4.3.4 的实践）。

**(f) Makefile 证实「无后端对象」。** CPU 构建的 `CPU_CORE_OBJS` 里**没有**任何后端 `.o`：

```makefile
CORE_OBJS = ds4.o ds4_distributed.o ds4_ssd.o ds4_metal.o        # macOS 图构建
CPU_CORE_OBJS = ds4_cpu.o ds4_distributed.o ds4_ssd.o            # CPU 构建：无后端对象
...
ds4_cpu.o: ds4.c ds4.h ds4_ssd.h ds4_distributed.h ds4_gpu.h
	$(CC) $(CFLAGS) -DDS4_NO_GPU -c -o $@ ds4.c
```

来源：[Makefile:L20-L21](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L20-L21) 与 [Makefile:L190-L191](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L190-L191) —— 中文说明：CPU 构建把 `ds4.c` 用 `-DDS4_NO_GPU` 编成 `ds4_cpu.o`，且 `CPU_CORE_OBJS` 完全不含 `ds4_metal.o`/`ds4_cuda.o`/`ds4_rocm.o`——所以那些 `ds4_gpu_*` 符号在 CPU 二进制里根本不存在，链接期也不会有冲突。

#### 4.3.4 代码实践

**实践目标**：用编译期证据确认「CPU 构建在源码层把整套 GPU 路径切除了」。

**操作步骤（可运行 / 源码阅读型）**：

1. 数一下切除点的规模。在仓库根目录执行：
   ```bash
   grep -c 'DS4_NO_GPU' ds4.c
   ```
   预期会得到一个相当大的数字（数十处），说明 CPU 构建在编译期通过 `#ifdef` 大量排除了 GPU 代码。
2. 阅读 [Makefile:L70-L75](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L70-L75) 的 `cpu:` 目标，确认它链接的是 `$(CPU_CORE_OBJS)`（无后端对象）且用 `$(LDLIBS)`（无 Metal / CUDA / ROCm 链接库）。
3. （可选，待本地验证）在没有 GPU 的 Linux 机器上执行 `make cpu`，再 `./ds4 --inspect`，观察 stderr 是否打印 `ds4: using CPU generation ...` 相关信息与默认后端 `cpu`。

**需要观察的现象**：`grep -c` 输出很大；`cpu:` 目标的链接行里没有任何 GPU 库；`default_backend()` 在该构建下返回 CPU。

**预期结果**：你能向别人解释——ds4 的 CPU 二进制是一个**瘦身版**，它不含任何 GPU 代码或库，靠 `#ifdef DS4_NO_GPU` 在编译期切除、靠不链接后端对象在链接期排除。

#### 4.3.5 小练习与答案

**练习 1**：CPU 后端为什么**不**实现 `ds4_gpu.h` 里的算子声明？

> **答案**：因为 CPU 参考路径走的是宿主 `float *` + `ds4_kv_cache` + `prefill_layer_major_cpu` 这套独立代码（[ds4.c:L22802](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22802)），由 `ds4_backend_uses_graph(CPU)==false` 直接分发（[ds4.c:L25158-L25193](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25158-L25193)）。它不需要 tensor-resident 抽象——宿主代码本来就天然常驻、天然零拷贝。

**练习 2**：一个 `make cpu` 出来的 `ds4` 二进制，里面能找到 `ds4_gpu_tensor_alloc` 这个符号吗？

> **答案**：不能。CPU 构建的 `CPU_CORE_OBJS` 不含任何后端对象（[Makefile:L21](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L21)），`ds4_gpu_tensor_alloc` 的实现只存在于 `ds4_metal.o`/`ds4_cuda.o`/`ds4_rocm.o` 里；同时 `ds4_cpu.o` 里所有对它的调用都被 `#ifndef DS4_NO_GPU` 切除。所以该符号既无定义也无引用。

**练习 3**：`ds4_session_is_cpu(s)` 在 [ds4.c:L23577](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L23577) 附近被用来做什么判断？它如何区分 CPU session 与图后端 session？

> **答案**：它判断 `s->engine->backend == DS4_BACKEND_CPU`（见 [ds4.c:L23577](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L23577) 上下文）。CPU session 没有图后端的 `ds4_gpu_graph` 状态（没有设备张量、没有逐层 device KV），所以像「分布式层快照」这类依赖图状态的功能会对 CPU session 直接拒绝（[ds4.c:L23672-L23675](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L23672-L23675)）。

---

## 5. 综合实践

**任务**：为 ds4 的「设备数据」画一张分类总览表，把本讲三个模块串起来。

请按下面的表格逐行填写，每一行都要写出**它由谁分配 / 它如何被设备读到 / 它是否常驻 / 对应的源码证据**：

| 数据类别 | 例子 | 分配方式 | 设备如何读 | 是否常驻 | 源码证据 |
| --- | --- | --- | --- | --- | --- |
| 激活 / KV / scratch | 某 layer 的 hidden、raw KV 行 | `ds4_gpu_tensor_alloc` | 算子间设备内交接 | 是（tensor-resident） | [ds4_gpu.h:L11-L20](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L11-L20) |
| 模型权重 | attention 投影、expert 张量 | 不分配，注册 model map | 按 `model_map + weight_offset` 零拷贝寻址 | 是（mmap 常驻） | [ds4_gpu.h:L224-L232](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L224-L232)、[ds4_metal.m:L7148-L7189](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L7148-L7189) |
| PRO Q4 expert 查找表 | 每层的 384-expert 地址表 | `ds4_gpu_preload_q4_expert_tables` 引擎打开时一次性建 | MoE 内核按 expert id 查表 | 是（预加载） | [ds4_gpu.h:L67-L71](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L67-L71)、[ds4.c:L25519-L25531](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25519-L25531) |
| CPU 路径的全部数据 | `ds4_kv_cache`、`float *logits` | 宿主 `xmalloc` / `kv_cache_init` | 宿主函数直接读写 | 天然常驻（宿主内存） | [ds4.c:L22802-L22842](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L22802-L22842) |

填完后，用一段话回答：**为什么权重用 model map，而激活用 `ds4_gpu_tensor`？** 参考答案——权重体积巨大（几十到几百 GB）且全程只读，注册一次零拷贝映射后按偏移寻址最省；激活/KV/scratch 体积小得多、且要被算子反复读写、生命周期与一次生成绑定，所以用 `ds4_gpu_tensor` 显式管理其 alloc/free 与命令缓冲内交接。

## 6. 本讲小结

- `ds4_gpu.h` 用 **tensor-resident** 模型统一了三个 GPU 后端：激活、KV、scratch 一旦在设备上分配就全程留在设备上，算子之间用设备指针交接，宿主只在头尾碰少量数据。
- 设备张量 `ds4_gpu_tensor` 是不透明类型，对外只暴露原语：`alloc / alloc_managed / view / free`（分配）、`bytes / contents`（查询）、`write / read / fill_f32`（宿主↔设备）、`copy / copy_f32_to_f16`（设备↔设备）。
- 命令缓冲有 `begin_commands → flush_commands / end_commands / synchronize` 生命周期；`flush` 的「提交一批、再开一批」用于让设备执行与宿主录制重叠。
- **model map** 把 mmap 出来的模型权重注册一次给设备（Metal 包 shared buffer / CUDA 用 `cudaHostRegisterMapped`），之后算子按 `model_map + weight_offset` 零拷贝寻址，避免每次上传巨额权重。
- **Q4 expert table** 在引擎打开时为 PRO 模型每层预建 384-expert 查找表，让 MoE 内核按 id 廉价索引。
- **CPU 参考后端** 是一条完全独立的宿主 C 路径（`generate_raw_swa_cpu`），不实现 `ds4_gpu.h`；由 `-DDS4_NO_GPU` 在编译期切除所有 GPU 代码、链接期不含任何后端对象，`default_backend()` 据此返回 CPU。

## 7. 下一步学习建议

本讲建立了「公共抽象 + CPU 参考」的地基，接下来三个方向任选其一深入：

- **进 Metal 后端**：下一篇 u5-l2 会讲 [ds4_metal.m](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m) 如何管理 Metal 设备/命令队列/管道、如何编译 `metal/*.metal` 内核、以及 layer-major 图调度——你会看到本讲的 `begin_commands/end_commands` 在真实 prefill 里是如何被编排的。
- **进 CUDA/ROCm 后端**：u5-l3 讲 [ds4_cuda.cu](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cuda.cu) 与 [ds4_rocm.cu](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_rocm.cu)，重点对照本讲的 `cudaHostRegisterMapped` model map 与三后端如何复用同一套 `ds4_gpu.h`。
- **进 SSD 流式**：如果你对 model map 的 `_spans` 变体感兴趣，可跳到 u9-l1/u9-l2，看 routed expert 如何按 span 从磁盘按需读入设备缓存。
