# GlobalTensor 与 LocalTensor 数据结构

## 1. 本讲目标

上一讲（u3-l1）我们建立了 Ascend 的内存层级与地址空间限定符的全局认知：数据住哪级存储、走哪条搬运路。本讲打开 u2-l3 里 `GlobalTensor`/`LocalTensor` 这两个黑盒，回答三个问题：

1. `GlobalTensor` 与 `LocalTensor` 各自**描述什么、内部装了什么**？
2. 一个 Tensor 是**怎么被创建出来的**（绑定 GM 指针 vs 由分配器分配片上内存）？
3. 它们有哪些**常用方法**（`SetGlobalBuffer`、`GetSize`、`GetValue`/`SetValue`、`operator()`、`operator[]` 等），以及**标量访问**和**批量搬运**的本质差异。

学完后你应当能够：读懂 add 样例里每一个 Tensor 调用；区分「描述 GM 的 GlobalTensor」和「描述片上 UB 的 LocalTensor」；并能动手把一次 GM 取数从「批量搬运」改成「标量逐元素取值」，说清两者的差异与适用场景。

## 2. 前置知识

本讲默认你已经掌握 u3-l1 的内容，重点回顾三条结论：

- **多级存储**：GM 是片外大容量内存（全核共享、慢）；UB/L1 是片上每核独享的快速存储，其中 UB 服务 Vector 通路。
- **两套命名**：硬件层用 `Hardware`（物理，如 `Hardware::UB`），软件层用 `TPosition`（逻辑，如 `TPosition::GM`/`TPosition::VECIN`），由 `GetPhyType` 桥接。
- **限定符被封装**：C++ Tensor 风格把 `__gm__`/`__ubuf__` 等地址空间限定符藏进了 `GlobalTensor`/`LocalTensor` 内部，我们不再裸写指针，而是操作这两个类型。

一个关键直觉先建立起来：

> **Tensor 不是「数据本身」，而是「一段存储的描述符」**——它记录「这段数据从哪个地址开始、有多长、放在哪级存储、是什么元素类型」。真正的内存由别处提供：GlobalTensor 的内存是 Host 通过 ACL 下发到 GM 的（已存在，只需绑定）；LocalTensor 的内存由分配器在片上现分配。

此外复习一个 u2-l3 的约束：add 样例里 `totalLength == numBlocks × blockLength`，本讲会反复用到这个切分。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [include/basic_api/kernel_tensor.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h) | `GlobalTensor`、`LocalTensor`、`LocalMemAllocator` 三个类的**声明**（公开接口头文件，本讲主战场） |
| [impl/basic_api/kernel_tensor_impl.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h) | 上述类的**实现**（内部头文件，看方法具体怎么写存储） |
| [impl/basic_api/kernel_tensor_base.h](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_base.h) | 基类 `BaseLocalTensor`/`BaseGlobalTensor`，定义了最底层的成员字段（地址、长度） |
| [examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc) | 矢量加法样例，本讲所有代码实践的修改对象 |

> 提示：`impl/` 下的文件是内部头文件（开头的 `#pragma message` 明确禁止直接 include），我们只读它们来理解实现，写算子时永远 include 公开的 `kernel_tensor.h`（或其聚合头 `kernel_operator.h`）。

## 4. 核心概念与源码讲解

### 4.1 GlobalTensor：GM 的一段「视图」

#### 4.1.1 概念说明

`GlobalTensor<T>` 是对 **GM（片外全局内存）上一段连续区域**的类型化描述符。你可以把它理解成一个「带长度和类型的 GM 指针包装器」。

它有两个最关键的特征：

1. **不分配内存**。GM 上的内存是 Host 侧用 `aclrtMalloc` 申请好、再通过 Kernel 入参（`__gm__ T*`）传进来的。`GlobalTensor` 只是**绑定**这个已经存在的指针，自己不开辟新空间。
2. **类型 + 长度感知**。裸的 `__gm__ float*` 只知道起点，不知道有多长、当什么类型用；`GlobalTensor<float>` 同时记录了元素类型 `T` 和元素个数 `bufferSize_`。

它和 u3-l1 的联系：`GlobalTensor` 内部持有的指针就是带 `__gm__` 限定符的，封装后你在上层看不到 `__gm__`，但底层仍指向 GM。

#### 4.1.2 核心流程

一个 `GlobalTensor` 的典型生命周期是「声明 → 绑定 → 使用」三步：

```text
1. 声明      GlobalTensor<float> xGm;        // 空 Tensor，内部指针为空
2. 绑定      xGm.SetGlobalBuffer(ptr, len);  // 把 GM 指针 + 元素个数记下来
3. 使用      DataCopy(local, xGm, len);      // 批量搬运
            或 xGm.GetValue(i);              // 标量取一个值
            或 xGm.SetValue(i, v);           // 标量写一个值
```

绑定时不拷贝数据，只记下两个信息：起始地址（记入 `address_`）和元素个数（记入 `bufferSize_`）。后续所有方法都基于这两个字段计算。

#### 4.1.3 源码精读

先看类的成员字段，理解它「装了什么」。[BaseGlobalTensor](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_base.h#L231-L257) 定义了两个 `__gm__` 指针成员：

```cpp
__gm__ PrimType* address_;      // 实际使用的地址（可能被 L2 cache 提示改写）
__gm__ PrimType* oriAddress_;   // 原始地址（保留绑定时的原始指针）
```

`GlobalTensor` 自身又加了一个长度字段和一个缓存模式字段（[kernel_tensor.h:289-295](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h#L289-L295)）：

```cpp
uint64_t bufferSize_;           // 元素个数（SetGlobalBuffer 的第二个参数）
CacheMode cacheMode_ = CacheMode::CACHE_MODE_NORMAL;
```

所以一个 `GlobalTensor` 的全部状态就是：**起点地址 + 元素个数 + 元素类型 + 缓存模式**。

再看绑定接口 `SetGlobalBuffer` 的两个重载（[kernel_tensor.h:265-266](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h#L265-L266)）：一个带长度、一个不带。带长度的实现（[kernel_tensor_impl.h:1251-1274](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1251-L1274)）核心就两件事：

```cpp
this->address_ = buffer;     // 记录起始地址（忽略不同芯片的 L2 cache 分支）
bufferSize_ = bufferSize;    // 记录元素个数
```

> 注：实现里有 `__NPU_ARCH__ == 3510` 等 L2 cache 相关分支，初学可忽略，只需记住「本质就是记地址 + 记长度」。

`GetSize()` 直接返回记录的元素个数（[kernel_tensor_impl.h:1644-1648](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1644-L1648)）：

```cpp
uint64_t GlobalTensor<T>::GetSize() const { return bufferSize_; }
```

注意 `GetSize()` 返回的是 `uint64_t`（因为 GM 很大），这点和 LocalTensor 不同，后面会对比。

add 样例里的真实用法（[add.asc:32-35](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L32-L35)）：

```cpp
AscendC::GlobalTensor<float> xGm, yGm, zGm;
xGm.SetGlobalBuffer(x + block_idx * blockLength, blockLength);  // 本核只绑定自己那一段
yGm.SetGlobalBuffer(y + block_idx * blockLength, blockLength);
zGm.SetGlobalBuffer(z + block_idx * blockLength, blockLength);
```

这里用 `x + block_idx * blockLength` 做指针偏移，正是 u6-l1「核间数据切分」的做法：每个核只绑定属于自己的那一段 GM，互不重叠。`matmul_advanced_api.asc` 里的注释也印证了第二个参数是元素个数（[matmul_advanced_api.asc:36,39](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/02_matrix/matmul_advanced_api/matmul_advanced_api.asc#L36-L39)）：「`SetGlobalBuffer` 的第二个参数是元素个数」。

#### 4.1.4 代码实践

**实践目标**：亲手验证「GlobalTensor 只是绑定，不拷贝」。

**操作步骤**（源码阅读型）：

1. 打开 [add.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc)，找到第 32-35 行。
2. 回到 Host 侧 `kernel_add`（第 82-90 行），确认 `xDevice/yDevice/zDevice` 是 `aclrtMalloc` 申请的 GM 指针，再经 `<<<>>>` 传给 Kernel 入参 `x/y/z`。
3. 追踪数据身份：`aclrtMalloc → x(__gm__ float*) → xGm.SetGlobalBuffer(x+...) → DataCopy(xLocal, xGm, ...)`。整条链路上 GM 里只有一份数据，`GlobalTensor` 没有复制它。

**需要观察的现象**：`xGm` 的地址（`x + block_idx*blockLength`）和 Host 申请的 `xDevice` 指向**同一块物理 GM**，没有中间拷贝。

**预期结果**：你能画出「Host `aclrtMalloc` 的 GM ←→ Kernel `__gm__` 入参 ←→ `GlobalTensor.address_`」三者其实是同一个地址的对应关系。

> 待本地验证：若在 Kernel 里加一行 `AscendC::printf("xGm size=%llu\n", xGm.GetSize());`（需 CPU/NPU 调试模式），应打印出 `blockLength`（2048）。

#### 4.1.5 小练习与答案

**练习 1**：`SetGlobalBuffer` 有两个重载（带长度 / 不带长度）。若用不带长度的重载绑定，`GetSize()` 会返回什么？

**答案**：返回 `bufferSize_` 的初始值。在 CPU 调试模式下不带长度的重载会把 `bufferSize_` 置 0（见 [kernel_tensor_impl.h:1297-1299](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1297-L1299)）。所以**需要长度信息时务必用带长度的重载**。

**练习 2**：`GlobalTensor` 的析构需要 `aclrtFree` 它绑定的 GM 吗？

**答案**：不需要，也做不到。`GlobalTensor` 不持有 GM 所有权，GM 的申请与释放都在 Host 侧（add 样例第 96-98 行的 `aclrtFree`）。Kernel 里的 `GlobalTensor` 随 Kernel 结束自动销毁，它只是个轻量描述符。

---

### 4.2 LocalTensor：片上存储的一段「已分配缓冲」

#### 4.2.1 概念说明

`LocalTensor<T>` 是对 **片上本地存储（默认 UB，也可以是 L1/TSCM 等）上一段已分配区域**的类型化描述符。

它和 `GlobalTensor` 最大的区别在于**内存从哪来**：

- `GlobalTensor`：GM 内存已存在，只需**绑定**。
- `LocalTensor`：片上内存需要**现分配**——由 `LocalMemAllocator`（基础 API）或 `TPipe/TQue`（框架 API，见 u5-l1）从该核独享的 UB 池里切一段出来给你。

它的内部状态比 GlobalTensor 复杂一点，因为要记录「在哪级片上存储」。核心成员是一个 `TBuffAddr` 结构（[kernel_tensor_base.h:107-115](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_base.h#L107-L115)）：

```cpp
struct TBuffAddr {
    uint32_t dataLen;        // 这段缓冲的字节长度
    uint32_t bufferAddr;     // 片上缓冲的偏移地址
    TBufHandle bufferHandle; // 缓冲句柄（TQue 管理时用）
    uint8_t   logicPos;      // 逻辑位置（即 TPosition：UB / L1 / ...）
};
```

其中 `logicPos` 正是 u3-l1 讲的 `TPosition`，记录这块缓冲住在哪级片上存储。

#### 4.2.2 核心流程

基础 API 下 LocalTensor 的典型生命周期是「分配器分配 → 使用 → （随核结束释放）」：

```text
1. 建分配器   LocalMemAllocator<Hardware::UB> ubAllocator;
2. 分配       LocalTensor<float> xLocal = ubAllocator.Alloc<float, blockLength>();
              // 分配器内部：在 head_ 处切出 blockLength*sizeof(float) 字节，
              //             记录 logicPos=UB，head_ 前移
3. 使用       DataCopy(xLocal, xGm, blockLength);   // 搬入
              Add(zLocal, xLocal, yLocal, blockLength);  // 矢量计算
              xLocal.GetValue(i);  /  xLocal.SetValue(i, v);  // 标量访问
```

分配器是一个简单的「栈式 bump allocator」：维护一个游标 `head_`，每次 `Alloc` 从 `head_` 处切出请求的字节数，再把 `head_` 往前推。

#### 4.2.3 源码精读

`LocalMemAllocator` 的分配逻辑（[kernel_tensor_impl.h:1755-1764](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1755-L1764)）把上面流程精确实现：

```cpp
template <TPosition pos, class DataType, uint32_t tileSize>
LocalTensor<DataType> LocalMemAllocator<hard>::Alloc()
{
    static_assert(GetPhyType(pos) == hard, "logic pos and hardware pos not matched.");
    LocalTensor<DataType> output(pos, head_, tileSize);        // 用 (pos, addr, size) 构造
    head_ += SizeOfBits<DataType>::value * tileSize / 8;       // 游标前移（按字节）
    return output;
}
```

注意 `static_assert(GetPhyType(pos) == hard)`：这把 u3-l1 的两套命名桥接起来了——你声明的逻辑位置 `pos`（TPosition）必须和分配器的硬件位置 `hard`（Hardware）一致，靠 `GetPhyType` 转换后比对。

`LocalTensor` 的构造函数（[kernel_tensor.h:189](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h#L189)）签名是 `(TPosition pos, uint32_t addr, uint32_t tileSize)`，最终落到 `CreateTensor`，把 `dataLen`、`bufferAddr`、`logicPos` 三个字段填好（[kernel_tensor_impl.h:1083-1085](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1083-L1085)）：

```cpp
this->address_.dataLen   = SizeOfBits<U>::value * tileSize / 8;  // 字节长度
this->address_.bufferAddr = addr;
this->address_.logicPos   = static_cast<uint8_t>(pos);
```

注意这里 `dataLen` 存的是**字节数**，不是元素个数。`GetSize()` 就是从字节长度反算元素个数（[kernel_tensor_impl.h:988-1006](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L988-L1006)），对常见的整字节类型（float/half/int）：

\[
\text{GetSize()} = \frac{\text{dataLen}}{\text{sizeof}(T)}
\]

这与 `GlobalTensor::GetSize()`（直接返回记录的 `bufferSize_`）的来源完全不同——一个靠「绑定时的个数」，一个靠「字节数换算」。

add 样例的真实用法（[add.asc:37-40](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L37-L40)）：

```cpp
AscendC::LocalMemAllocator<AscendC::Hardware::UB> ubAllocator;
AscendC::LocalTensor<float> xLocal = ubAllocator.Alloc<float, blockLength>();
AscendC::LocalTensor<float> yLocal = ubAllocator.Alloc<float, blockLength>();
AscendC::LocalTensor<float> zLocal = ubAllocator.Alloc<float, blockLength>();
```

三块 UB 缓冲在 `head_` 上依次排开，互不重叠。注意元素下标类型是 `uint32_t`（片上存储小，下标够用），而 GlobalTensor 是 `uint64_t`。

#### 4.2.4 代码实践

**实践目标**：理解「栈式分配」导致的地址连续性，并体会 UB 容量约束。

**操作步骤**（源码阅读型 + 参数修改）：

1. 读 [add.asc:37-40](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc#L37-L40)，注意三块缓冲依次分配，因此 `xLocal`、`yLocal`、`zLocal` 在 UB 里地址相邻。
2. 想象把模板实参 `blockLength` 改大（例如 4096），三块缓冲总占用 `3 × 4096 × 4 = 48 KB`。
3. 查阅：每核 UB 容量有限（典型 192~256 KB 量级，随芯片不同）。若三块缓冲之和超过 UB 容量，编译/运行会报 buffer overflow（见 `CreateTensor` 里的 `ASCENDC_DEBUG_ASSERT`，[kernel_tensor_impl.h:1055-1079](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1055-L1079)）。

**需要观察的现象**：`blockLength` 增大时，UB 占用线性增长；超过容量后 CPU 调试模式直接断言失败。

**预期结果**：你能说出「LocalTensor 的总大小受限于片上 UB 容量，这就是为什么大算子要 Tiling（u6-l2）分块搬运」的根本原因。

> 待本地验证：在 CPU 调试模式下把 `blockLength` 改成一个明显超容的值，编译运行应触发 `buffer overflow` 断言。

#### 4.2.5 小练习与答案

**练习 1**：`LocalTensor` 和 `GlobalTensor` 的元素下标类型分别是什么？为什么不同？

**答案**：LocalTensor 用 `uint32_t`，GlobalTensor 用 `uint64_t`（对比 [kernel_tensor.h:176](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h#L176) 与 [kernel_tensor.h:270](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h#L270)）。因为片上 UB 容量小，`uint32_t` 足够寻址；GM 可达数 GB，必须用 `uint64_t`。

**练习 2**：为什么 `LocalMemAllocator` 构造函数里要 `static_assert(hard != Hardware::GM)`？

**答案**：见 [kernel_tensor_impl.h:1737](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1737)。`LocalMemAllocator` 是分配**片上本地**内存的，GM 不是片上存储、也不由 Kernel 端分配器管理（GM 由 Host 的 ACL 管理），所以禁止对 GM 使用。

---

### 4.3 Tensor 常用方法：GetSize、GetValue/SetValue 与访问范式

#### 4.3.1 概念说明

GlobalTensor 和 LocalTensor 共享一批同名方法，但**含义和底层通路不同**。本模块把这些常用方法集中讲清，重点抓住一个对算子性能至关重要的区分：

> **批量搬运（DataCopy）走 MTE2/MTE3 搬运流水线，吞吐高**；**标量访问（GetValue/SetValue）走 S（Scalar）标量流水线，吞吐低**。

`GetValue`/`SetValue` 的声明里都带 `__inout_pipe__(S)` 标注（[kernel_tensor.h:270](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h#L270)、[kernel_tensor.h:176](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/include/basic_api/kernel_tensor.h#L176)），这个 `S` 就是「标量流水线」。它是为「取一两个标量做控制判断」「不规则下标逐元素搬」这类场景设计的，不是用来批量算数据的。

下表汇总常用方法（两类型共有者行为类似但通路不同）：

| 方法 | GlobalTensor | LocalTensor | 作用 |
|---|---|---|---|
| `SetGlobalBuffer(ptr[, size])` | ✅ 绑定 GM 指针 | ❌ | 建立 GM 视图 |
| `GetSize()` | 返回 `bufferSize_`（uint64） | 返回 `dataLen/sizeof(T)`（uint32） | 元素个数 |
| `GetValue(offset)` | 从 GM 标量读一个（S 管线） | 从 UB 标量读一个（S 管线） | 标量取值 |
| `SetValue(offset, v)` | 向 GM 标量写一个 | 向 UB 标量写一个 | 标量赋值 |
| `operator()(offset)` | 返回 `__gm__ T&` 引用 | 返回 `__ubuf__ T&` 引用 | 可读可写的元素引用 |
| `operator[](offset)` | 返回偏移后的子 Tensor | 返回偏移后的子 Tensor | 切片视图 |
| `GetPhyAddr([offset])` | `__gm__ T*` | 片上地址（uint64） | 取物理地址 |
| `ReinterpretCast<U>()` | 仅 3510/5102 | ✅ | 类型重解释 |
| `SetShapeInfo`/`GetShapeInfo` | ✅ | ✅ | 形状信息（ND/NZ） |

#### 4.3.2 核心流程

「批量」与「标量」两条访问路径对照：

```text
【批量路径 —— 推荐，吞吐高】
  GM ──DataCopy(MTE2)──> LocalTensor(UB) ──Add/Exp(M,矢量管线)──> LocalTensor ──DataCopy(MTE3)──> GM
  一次搬运一整块，对齐友好，硬件高度并行

【标量路径 —— 仅用于少量/不规则访问】
  GM.GetValue(i) ──S 标量管线──> 标量寄存器 ──逐元素运算──> SetValue(i, v)
  一次一个元素，吞吐低，但能表达任意下标（如 gather/scatter）
```

`operator()(offset)` 是 `GetValue`/`SetValue` 的「引用版」：`xGm(i)` 返回的是 `__gm__ T&`，既能读 `v = xGm(i)` 也能写 `xGm(i) = v`，底层等价于标量访问。

`operator[](offset)` 则返回一个新的 Tensor，其地址在原 Tensor 基础上偏移 `offset` 个元素（[kernel_tensor_impl.h:1650-1675](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1650-L1675)），相当于「取后半段的视图」，不拷贝数据。

#### 4.3.3 源码精读

**`GlobalTensor::GetValue`（标量读 GM）** 的非 3510 主分支（[kernel_tensor_impl.h:1495-1500](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1495-L1500)）极其简洁：

```cpp
} else {
    return this->oriAddress_[offset];   // 直接取 GM 上第 offset 个元素
}
```

正是「标量读一个 GM 元素」。对比 `DataCopy` 走 MTE2 搬运单元、一次搬一整块，这里一次只取一个。

**`GlobalTensor::SetValue`（标量写 GM）** 同理（[kernel_tensor_impl.h:1616-1621](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1616-L1621)）：

```cpp
} else {
    this->oriAddress_[offset] = value;  // 直接写 GM 上第 offset 个元素
}
```

**真实工程用例**：scatter 算子在不支持 Scatter 指令的芯片上，正是用标量 `GetValue`/`SetValue` 循环逐元素搬出（[scatter_custom.asc:47-52](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/06_compatibility_guide/scatter/scatter_custom.asc#L47-L52)）：

```cpp
// Atlas A2/A3不支持Scatter指令，使用标量GetValue/SetValue循环逐元素搬出
for (int32_t i = 0; i < COUNT; ++i) {
    auto offset = dstOffsetLocal.GetValue(i) / sizeof(T);  // 不规则下标
    auto srcValue = srcLocal.GetValue(i);
    dstLocal.SetValue(offset, srcValue);                   // 散写到任意位置
}
```

这条注释点明了标量访问的**适用场景**：当下标不规则（gather/scatter）时，批量搬运表达不了，只能逐元素标量访问。这是本讲综合实践的理论依据。

**`LocalTensor::operator()`（引用）** 返回 `__ubuf__ T&`（[kernel_tensor_impl.h:810-815](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L810-L815)），和 `GlobalTensor::operator()` 返回 `__gm__ T&` 形成对照——限定符不同，因为底层存储不同。

#### 4.3.4 代码实践

**实践目标**：亲手对比 `operator[]` 切片与 `GetSize`。

**操作步骤**（源码阅读型）：

1. 读 [kernel_tensor_impl.h:1650-1675](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/impl/basic_api/kernel_tensor_impl.h#L1650-L1675) 的 `GlobalTensor::operator[]`。
2. 推理：在 add 样例里，若写 `auto xGmHalf = xGm[blockLength/2];`，则 `xGmHalf.GetPhyAddr()` 应指向 `x + block_idx*blockLength + blockLength/2`，且 `xGmHalf.GetSize()` 仍为 `blockLength`（注意 operator[] 只移动地址、不修改 `bufferSize_`）。

**需要观察的现象**：切片后地址偏移了半个块，但记录的长度字段未缩减——这说明 `operator[]` 是「带原长度的偏移视图」，使用时要自己注意有效范围。

**预期结果**：你能解释为什么 `operator[]` 适合「从某偏移继续处理」而不是「精确截断」。

> 待本地验证：可在 CPU 调试模式下 `printf` 打印 `xGmHalf.GetPhyAddr()` 与 `xGm.GetPhyAddr()` 的差值，应为 `blockLength/2 * sizeof(float)` 字节。

#### 4.3.5 小练习与答案

**练习 1**：add 样例里为什么用 `DataCopy(xLocal, xGm, blockLength)` 批量搬运，而不用 `for (i) xLocal.SetValue(i, xGm.GetValue(i));` 循环？

**答案**：因为 `DataCopy` 走 MTE2 搬运流水线，一次搬一整块、高度并行；而 `GetValue`/`SetValue` 走 S 标量流水线，一次一个元素、吞吐低。对连续大块数据，批量搬运的性能远高于标量循环。标量访问只在不规则下标（如 scatter）或只取一两个标量做控制时才用。

**练习 2**：`operator()(offset)` 和 `operator[](offset)` 有什么区别？

**答案**：`operator()(offset)` 返回第 `offset` 个**元素的引用**（`T&`），读写单个值；`operator[](offset)` 返回**偏移后的子 Tensor**（仍是 Tensor 类型），用于从某偏移开始看一段视图。前者操作一个元素，后者操作一段区域。

---

## 5. 综合实践

把 4.1～4.3 串起来，完成一个有对比意义的改造。

**任务**：基于 [add.asc](https://github.com/gitcode.com/cann/asc-devkit/blob/4952d23d863568f8976789364b7af331909bb993/examples/01_simd_cpp_api/00_introduction/01_add/add/add.asc)，把其中一个输入（`x`）改用 `GlobalTensor::GetValue` 直接在 GM 上逐元素取值，另一个输入（`y`）仍走原来的 `DataCopy` 搬运到 UB。对比两种 GM 访问方式。

**操作步骤**：

1. 复制一份 `add.asc`，在 Kernel 内做如下改造（示例代码，仅替换原第 42-46 行的计算段）：

   ```cpp
   // 示例代码：y 走批量搬运（原样），x 改为标量直接从 GM 取
   AscendC::DataCopy(yLocal, yGm, blockLength);     // y：MTE2 批量搬运
   AscendC::PipeBarrier<PIPE_ALL>();

   for (uint32_t i = 0; i < blockLength; ++i) {
       float xv = xGm.GetValue(i);                  // x：S 标量流水线，逐个从 GM 读
       float yv = yLocal.GetValue(i);               // 从 UB 标量读
       zLocal.SetValue(i, xv + yv);                 // 标量写回 UB
   }
   AscendC::PipeBarrier<PIPE_ALL>();
   AscendC::DataCopy(zGm, zLocal, blockLength);     // 结果批量搬回 GM
   ```

2. `xLocal` 这块 UB 缓冲此时不再需要，可删去对应的 `Alloc`（体会 LocalTensor 由分配器管理、不用就别占 UB）。
3. 保持 `totalLength == numBlocks × blockLength` 不变，按 u2-l3 的流程编译运行。

**需要观察的现象**：

- **结果正确性**：输出应与原样例完全一致（`test pass!`），因为 `xGm.GetValue(i)` 取到的就是原 `xLocal` 里被 DataCopy 搬进来的同一个值——这印证了 4.1 的结论「GlobalTensor 绑定的就是 GM 原址」。
- **性能差异**：标量循环版应明显慢于原批量版（待本地用 profiler 量化）。

**预期结果与差异说明**：

| 维度 | 原 add（两输入都 DataCopy） | 改造版（x 用 GetValue） |
|---|---|---|
| GM 访问通路 | MTE2 批量搬运（高吞吐） | S 标量流水线（低吞吐） |
| 是否占 UB | 占 3 块（x/y/z） | 占 2 块（y/z），省一块 |
| 计算方式 | `Add` 矢量指令（M 管线，并行） | 标量加法（逐元素循环） |
| 适用场景 | 连续大块矢量运算 | 仅当下标不规则/只取少量标量时 |

**结论**：`GetValue`/`SetValue` 直接访问 GM 在**功能上**可以替代搬运，但在**性能上**只适合少量、不规则的访问；连续大块数据务必用 `DataCopy` 批量搬运 + 矢量计算接口。这也正是 scatter 样例（4.3.3）只在「没有 Scatter 指令」时才退化为标量循环的原因。

> 待本地验证：建议在真机/仿真上用 msprof 等工具对比两版的 cycle 数，量化标量访问的开销。若无法运行，至少完成源码改造并保证编译通过（CPU 调试模式下 `GetValue` 可正常执行）。

## 6. 本讲小结

- `GlobalTensor<T>` 是 GM 上一段区域的**类型化视图**：不分配内存，靠 `SetGlobalBuffer` 绑定已存在的 `__gm__ T*` 与元素个数；`GetSize()` 返回记录的 `bufferSize_`（uint64）。
- `LocalTensor<T>` 是片上本地存储（默认 UB）一段**已分配缓冲**：由 `LocalMemAllocator`（基础 API）或 `TPipe/TQue`（框架 API）分配；内部用 `TBuffAddr{bufferAddr, dataLen, logicPos}` 描述，`GetSize()` 由 `dataLen/sizeof(T)` 换算（uint32）。
- 两者下标类型不同（uint64 vs uint32），根源是 GM 与 UB 容量量级不同。
- `GetValue`/`SetValue`/`operator()` 是**标量访问**，走 S 标量流水线，吞吐低，适合少量或不规则（gather/scatter）访问；连续大块数据应走 `DataCopy`（MTE2/MTE3）+ 矢量计算。
- `operator[](offset)` 返回偏移视图（移动地址、不改记录长度），用于「从某偏移继续处理」。
- 工程取舍：`GlobalTensor` 解决「描述 GM」、`LocalTensor` 解决「占用片上缓冲」，二者配合完成 GM↔UB 的数据流。

## 7. 下一步学习建议

- **紧接着学 u3-l3（DataCopy 与 LocalMemAllocator）**：本讲把 `DataCopy` 当黑盒带过，下一讲会打开它，讲清 GM↔UB 的批量搬运细节、对齐约束，以及 `LocalMemAllocator` 自主内存管理的完整范式。
- **对比 u5-l1（TPipe/TQue 框架）**：本讲的 `LocalMemAllocator` 是「自主管理内存」；u5-l1 的 `TQue` 用 `AllocTensor/EnQue/DeQue/FreeTensor` 队列语义自动管同步。学完后建议回头把 add 样例用两种方式各写一遍，体会差异。
- **延伸阅读**：`scatter_custom.asc` 与 `gm_by_dcache.asc` 是标量 GM 访问的真实工程用例，可作为本讲综合实践的参照。
