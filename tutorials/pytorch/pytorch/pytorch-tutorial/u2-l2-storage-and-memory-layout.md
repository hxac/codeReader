# Storage 与内存布局

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚一个 `torch.Tensor` 在底层由「数据（Storage）」和「视图描述（sizes / strides / storage_offset）」两部分拼出来，二者是分离的。
- 解释 `stride`、`storage_offset`、`sizes` 这三者如何共同定位一个 N 维张量里任意一个元素在内存中的位置。
- 看懂 `torch/storage.py` 里 `UntypedStorage` / `TypedStorage` 的封装关系，并知道为什么官方建议用 `tensor.untyped_storage()`。
- 在 C++ 层（`c10/core/StorageImpl.h`）找到 `data_ptr_` 与 `size_bytes_` 这两个字段，理解它们是 Storage 真正持有的东西。
- 用一段代码手动推算转置张量里某个元素的物理偏移，并理解 `contiguous()` / `view()` / `reshape()` 与内存布局的关系。

承接上一讲：u2-l1 已经说明 `torch.Tensor` 是对 C++ `TensorBase` 的薄包装，真正的算子和属性都在底层。本讲就钻进底层，回答「一个 Tensor 的数据到底存在哪里、又是怎么被解释成 N 维形状的」。

## 2. 前置知识

在进入源码前，先用一段直觉建立心智模型。

### 2.1 内存是一维的，张量是 N 维的

无论 CPU 还是 GPU，显存/内存对程序而言都是一段**一维字节序列**。但深度学习里我们用的是 0 维标量、1 维向量、2 维矩阵、4 维图像 batch……这中间的鸿沟怎么填？

PyTorch 的做法是：**数据本身是一维的，"几维" 只是看待数据的一种视角**。一段一维的字节缓冲区，配上「形状 `sizes`」和「步幅 `strides`」，就可以被解释成任意维度的张量，而**不需要搬动任何数据**。

你可以把这段一维缓冲区想象成一长条磁带：

```
磁带(Storage)：  [0][1][2][3][4][5][6][7][8][9][10][11]
                  ^
                  data_ptr（起点）
```

而张量则是「在这条磁带上，按某种跳跃规则读取」的说明。这条磁带就是 **Storage**，跳跃规则就是 **strides** 和 **storage_offset**。

### 2.2 三个关键词

| 名词 | 含义 | 单位 |
|------|------|------|
| `storage` | 底层那段一维字节/元素缓冲区（带一个起始指针和总长度） | — |
| `storage_offset` | 张量的"第 0 个元素"在 storage 里的偏移位置 | **元素个数**，不是字节 |
| `strides` | 沿每个维度移动一步时，要在 storage 里跳过多少个**元素** | 元素个数 |
| `sizes` | 每个维度的长度（就是 `tensor.shape`） | 元素个数 |

记住一个**最重要的换算公式**。给定一个 N 维下标 \((i_0, i_1, \dots, i_{n-1})\)，它对应 storage 里的元素位置是：

\[
\mathrm{offset} = \mathrm{storage\_offset} + \sum_{d=0}^{n-1} i_d \cdot \mathrm{stride}_d
\]

再用 dtype 的字节大小乘一下，就得到相对 `data_ptr` 的**字节偏移**。本讲后面会反复用到这个公式。

### 2.3 为什么要分离？

把「数据」和「视图」分开，最大好处是**很多操作零拷贝**。比如：

- 转置 `x.t()`：只是把 `sizes` 和 `strides` 交换一下，数据纹丝不动。
- 切片 `x[2:]`：只是把 `storage_offset` 加 2。
- `.view()`：只要新形状能用同一组 strides 表达，就只改 `sizes`。

如果数据和视图绑死，这些操作都得复制一整块内存，性能会差几个数量级。这就是 PyTorch 「strided tensor」设计的核心动机。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 角色 |
|------|------|
| `torch/storage.py` | Python 侧 Storage 封装：定义 `UntypedStorage`、`TypedStorage`（已废弃）以及公共基类 `_StorageBase`。 |
| `c10/core/StorageImpl.h` | C++ 真正持有「数据指针 + 字节数」的结构体 `StorageImpl`，是所有 storage 的内核。 |
| `c10/core/Storage.h` | 对 `StorageImpl` 的引用计数包装 `Storage`，方便在 C++ 里传来传去。 |
| `c10/core/TensorImpl.h` | Tensor 在 C++ 层的表示，持有 `Storage` + `sizes_and_strides_` + `storage_offset_`，是「视图」这一半的家。 |

一句话关系：`TensorImpl`（视图）持有 `Storage`，`Storage` 持有 `StorageImpl`（数据）。Python 侧的 `UntypedStorage` 再用 `_cdata` 指针去指 `StorageImpl`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先看 Python 侧 Storage 封装，再看 C++ `StorageImpl` 真正存了什么，最后讲 stride/offset 如何把数据解释成 N 维视图。

### 4.1 Storage / UntypedStorage 的 Python 封装

#### 4.1.1 概念说明

在 Python 里，你能直接摸到的那段"底层数据"叫 **Storage**。早些年 PyTorch 给每种 dtype 一个独立的 Storage 类（`FloatStorage`、`LongStorage`……），它们合起来叫 `TypedStorage`——**带 dtype 的** storage。

后来官方意识到这种设计会随 dtype 数量爆炸，于是引入了 **`UntypedStorage`**：它只关心"一段多长的字节缓冲区"，不关心里面是 float 还是 int。`TypedStorage` 现在已经**废弃**，源码里到处是删除警告，推荐用 `tensor.untyped_storage()` 直接拿 `UntypedStorage`。

文件顶部的导出就很清楚地说明了当前仅两个公开类：

```python
__all__ = ["TypedStorage", "UntypedStorage"]
```

参见 [torch/storage.py:23](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/storage.py#L23)，这一行确认了 Storage 模块对外只暴露这两个名字。

#### 4.1.2 核心流程

`UntypedStorage` 的继承关系很关键：

```python
class UntypedStorage(torch._C.StorageBase, _StorageBase):
```

参见 [torch/storage.py:476](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/storage.py#L476)。它有两个父类：

1. `torch._C.StorageBase`：C++ 通过 pybind11 注册的类型，提供 `data_ptr()`、`nbytes()`、`resize_()` 等真正干活的 C++ 方法。
2. `_StorageBase`：纯 Python 基类，补充 `clone()`、`cpu()`、`to()`、`__repr__` 等用 Python 写更方便的方法。

也就是说，`UntypedStorage` 的"重活"（拿到真实数据指针、知道字节数）全部委托给 C++ 的 `StorageBase`，Python 这边只做一层薄薄的便利封装。

`_StorageBase` 里有大量方法只写了一个 `raise NotImplementedError`，因为它们是给 C++ 覆盖的占位符。比如：

```python
def data_ptr(self) -> _int:
    raise NotImplementedError
```

参见 [torch/storage.py:127-128](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/storage.py#L127-L128)。运行时真正返回指针的是 C++ 那边注册到 `StorageBase` 上的同名方法，Python 这个声明只是为了让类型检查和文档系统知道有这个接口。

真正用 Python 实现的有用方法，比如 `clone()`（返回一份拷贝）：

```python
def clone(self):
    """Return a copy of this storage."""
    return type(self)(self.nbytes(), device=self.device).copy_(self)
```

参见 [torch/storage.py:262-264](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/storage.py#L262-L264)。它的逻辑很直白：按当前字节数新建一个同设备的 storage，再把数据 copy 过去。

#### 4.1.3 源码精读：TypedStorage 如何包裹 UntypedStorage

`TypedStorage` 已经废弃，但理解它的结构有助于看懂旧代码和 pickle 序列化路径。它的核心是**持有一个 `UntypedStorage` 外加一个 `dtype`**：

```python
class TypedStorage:
    is_sparse: _bool = False
    _fake_device: torch.device | None = None

    dtype: torch.dtype
```

参见 [torch/storage.py:685-690](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/storage.py#L685-L690)。注意它没有继承 `UntypedStorage`，而是用组合：在 `__init__` 里把真正的存储塞进 `self._untyped_storage`：

```python
            self.dtype = dtype
            ...
            self._untyped_storage = wrap_storage
```

参见 [torch/storage.py:843-851](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/storage.py#L843-L851)（关键字段 `self._untyped_storage = wrap_storage`）。

于是 `TypedStorage` 的几乎所有方法都是先 `_warn_typed_storage_removal()` 发警告，再把调用转发给内部的 `UntypedStorage`。典型如 `data_ptr()`：

```python
    def data_ptr(self):
        _warn_typed_storage_removal()
        return self._data_ptr()

    # For internal use only, to avoid deprecation warning
    def _data_ptr(self):
        return self._untyped_storage.data_ptr()
```

参见 [torch/storage.py:1262-1268](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/storage.py#L1262-L1268)。这正是「`TypedStorage` 只是个带 dtype 标签的 `UntypedStorage` 壳」的直接证据。

废弃警告本身长这样：

```python
        message = (
            "TypedStorage is deprecated. It will be removed in the future and "
            "UntypedStorage will be the only storage class. This should only matter "
            "to you if you are using storages directly.  To access UntypedStorage "
            "directly, use tensor.untyped_storage() instead of tensor.storage()"
        )
        warnings.warn(message, UserWarning, stacklevel=stacklevel + 1)
```

参见 [torch/storage.py:663-670](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/storage.py#L663-L670)。注意最后一句明确告诉我们：**拿底层数据用 `tensor.untyped_storage()`，不要用 `tensor.storage()`**。

> 小提示：`UntypedStorage` 也提供了反过来"假装成 Typed"的能力，`untyped()` 直接返回自身（见 [torch/storage.py:425-426](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/storage.py#L425-L426)），因为它的类型本来就是 Untyped。

#### 4.1.4 代码实践：从 Tensor 取出 Storage

实践目标：亲手验证 Tensor 与 Storage 是分离的，多个视图可以共享同一个 Storage。

操作步骤：

1. 创建一个张量并取出它的 `untyped_storage()`。
2. 用切片造一个"后半段"视图，比较两者的 `data_ptr()` 和 `storage_offset()`。
3. 在视图上修改一个元素，观察原张量是否也变了。

```python
import torch

x = torch.tensor([10, 20, 30, 40, 50], dtype=torch.int32)
s = x.untyped_storage()
print("storage nbytes :", s.nbytes())          # 5 个 int32 = 20 字节
print("x.storage_offset():", x.storage_offset())  # 0

y = x[2:]                                       # 切片：跳过前 2 个
print("y.storage_offset():", y.storage_offset())  # 预期 2
print("y.data_ptr() - x.data_ptr():", y.data_ptr() - x.data_ptr())  # 预期 8（2 个 int32）

y[0] = 999
print("x:", x)   # 观察 x[2] 是否也变成 999 —— 证明共享同一份 storage
```

需要观察的现象：

- `y.storage_offset()` 应为 `2`，`y.data_ptr()` 比 `x.data_ptr()` 大 `2 * 4 = 8` 字节。
- 修改 `y[0]` 后 `x` 的第 3 个元素也变了，因为它们指向**同一个** Storage，只是起点偏移不同。

预期结果：切片没有复制数据，只是改了 `storage_offset`。

### 4.2 c10 StorageImpl 的 data_ptr 与 size

#### 4.2.1 概念说明

剥开 Python 外壳，Storage 真正的灵魂在 C++ 的 `StorageImpl`。文件开头有一段很重要的注释，说明了 Storage 的本质和它的"唯一所有权"约定：

```cpp
// A storage represents the underlying backing data buffer for a
// tensor.  This concept was inherited from the original Torch7
// codebase; we'd kind of like to get rid of the concept
// (see https://github.com/pytorch/pytorch/issues/14797) but
// it's hard work and no one has gotten around to doing it.
//
// NB: storage is supposed to uniquely own a data pointer; e.g.,
// two non-null data pointers alias if and only if they are from
// the same storage.
```

参见 [c10/core/StorageImpl.h:32-39](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/StorageImpl.h#L32-L39)。这段话透露两点：

1. Storage 就是「张量背后的那段数据缓冲区」，是个从 Torch7 继承下来的老概念，团队其实想删掉它，但工作量大。
2. **storage 唯一拥有一个数据指针**——两个非空指针若相等（alias），它们一定来自同一个 storage。这是后续 deepcopy、版本计数（version counter）正确性的基础。

#### 4.2.2 核心流程

`StorageImpl` 继承自 `intrusive_ptr_target`，意味着它通过**侵入式引用计数**被多处共享（多个 Tensor 共享一个 storage 时，引用计数 +1）：

```cpp
struct C10_API StorageImpl : public c10::intrusive_ptr_target {
 public:
  struct use_byte_size_t {};
```

参见 [c10/core/StorageImpl.h:55-57](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/StorageImpl.h#L55-L57)。注意内嵌的 `use_byte_size_t` 标签类型——它是个"标签参数"，构造函数靠它来区分"我传进来的是字节数还是元素个数"。因为 `size_t` 本身没法区分，所以 PyTorch 用一个空 struct 当占位符：

```cpp
  StorageImpl(
      use_byte_size_t /*use_byte_size*/,
      SymInt size_bytes,
      at::DataPtr data_ptr,
      at::Allocator* allocator,
      bool resizable)
      : data_ptr_(std::move(data_ptr)),
        size_bytes_(std::move(size_bytes)),
        ...
        allocator_(allocator) { ... }
```

参见 [c10/core/StorageImpl.h:59-76](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/StorageImpl.h#L59-L76)。这就是核心构造函数：吃进**数据指针 `data_ptr`**、**字节数 `size_bytes`**、一个**分配器 `allocator`** 和**是否可扩容 `resizable`** 四件套。

获取字节数和指针的方法都很短，而且走的是"非堆分配快速路径"：

```cpp
  size_t nbytes() const {
    // OK to do this instead of maybe_as_int as nbytes is guaranteed positive
    TORCH_CHECK(!size_bytes_is_heap_allocated_);
    return size_bytes_.as_int_unchecked();
  }
```

参见 [c10/core/StorageImpl.h:115-119](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/StorageImpl.h#L115-L119)。`nbytes()` 直接返回 `size_bytes_` 的整数值。这里出现的 `SymInt` / `size_bytes_is_heap_allocated_` 是为动态形状（dynamic shape）服务的——形状可能是符号化整数，必须堆分配；普通张量则内联存一个 `int64_t`，所以这里能 `as_int_unchecked()` 高速取值。

拿数据指针（只读）同样简单，但多了一道"不可变检查"：

```cpp
  const at::DataPtr& data_ptr() const {
    if (C10_UNLIKELY(throw_on_immutable_data_ptr_)) {
      throw_data_ptr_access_error();
    }
    return data_ptr_;
  }
```

参见 [c10/core/StorageImpl.h:144-149](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/StorageImpl.h#L144-L149)。`throw_on_immutable_data_ptr_` 这种标志位是为 FakeTensor / CUDA Graph 失效等场景准备的——这些场景里 storage 根本没有真实数据，访问 `data_ptr()` 应该直接抛错而不是返回野指针。这也是 `torch/storage.py` 里 `_throws_on_data_ptr_access` 想要探测的行为（见 [torch/storage.py:41-47](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/storage.py#L41-L47)）。

#### 4.2.3 源码精读：StorageImpl 真正持有的字段

把视线移到文件底部的私有成员，就能看到 `StorageImpl` 的"全部家当"：

```cpp
  DataPtr data_ptr_;
  SymInt size_bytes_;
  bool size_bytes_is_heap_allocated_;
  bool resizable_;
  bool received_cuda_;
  bool has_mutable_data_ptr_check_ = false;
  bool throw_on_mutable_data_ptr_ = false;
  bool throw_on_immutable_data_ptr_ = false;
  bool warn_deprecated_on_mutable_data_ptr_ = false;
  MaterializeFn materialize_fn_ = nullptr;
  Allocator* allocator_;
  impl::PyObjectSlot pyobj_slot_;
  std::unique_ptr<StorageExtraMeta> extra_meta_ = nullptr;
```

参见 [c10/core/StorageImpl.h:391-412](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/StorageImpl.h#L391-L412)。剥掉一堆用于错误处理和 materialize 的开关，**真正描述数据本身的就是头两个字段**：

- `data_ptr_`：`at::DataPtr` 类型，它把"裸指针 + deleter + 所在 Device"打包在一起，是真正的内存句柄。
- `size_bytes_`：这段缓冲区有多少**字节**（注意不是元素个数）。

其余字段含义：

- `resizable_`：能否被 `resize_()` 扩容；可扩容就必须有 `allocator_`。
- `received_cuda_`：标记这个 storage 是从别的进程收到的（用于分布式 IPC，本地没有真实 CUDA 分配）。
- `allocator_`：当初分配这块内存的分配器指针，将来扩容要用。
- `pyobj_slot_`：用来挂"这个 Storage 对应的 Python `UntypedStorage` 对象"，方便 C++↔Python 双向找。
- `extra_meta_`：额外元信息（目前只放自定义的 data_ptr 错误消息）。

可以看到：**StorageImpl 只知道"一段多长字节、在哪台设备上、由谁分配"，完全不知道形状、stride、dtype 之外的语义**。这些"怎么解释数据"的职责，全部留给 `TensorImpl`（下一节会看到）。

#### 4.2.4 代码实践：观察 nbytes 与 data_ptr

实践目标：验证 `UntypedStorage.nbytes()` 走的就是 `StorageImpl::nbytes()`，且和 `元素数 × itemsize` 一致。

操作步骤：

1. 用不同 dtype 创建等长张量。
2. 打印各自 `untyped_storage().nbytes()` 与 `numel() * element_size()`。

```python
import torch

for dt in [torch.float32, torch.float16, torch.int8]:
    t = torch.zeros(1000, dtype=dt)
    s = t.untyped_storage()
    print(dt, "nbytes =", s.nbytes(),
          " numel*itemsize =", t.numel() * t.element_size())
```

预期结果：`nbytes` 与 `numel * itemsize` 完全相等，分别是 4000、2000、1000。这印证了 `StorageImpl` 用 `size_bytes_`（字节）而非元素数记录容量。

### 4.3 stride 与 offset 的语义

#### 4.3.1 概念说明

前面两节讲了"数据"这一半。现在讲"视图"这一半：`TensorImpl` 怎么用 `sizes` / `strides` / `storage_offset` 把一段一维 storage 解释成 N 维张量。

回忆 2.2 节的公式。元素下标 \((i_0,\dots,i_{n-1})\) 到 storage 偏移（按元素计）的映射是：

\[
\mathrm{offset} = \mathrm{storage\_offset} + \sum_{d=0}^{n-1} i_d \cdot \mathrm{stride}_d
\]

`stride_d` 的物理含义就是：**沿第 d 维走一步，要在 storage 里前进多少个元素**。对一个按行优先（row-major）存放的 contiguous 张量，最末维 stride=1，越靠前的维 stride 越大。

举一个 2×3 的行优先 int32 矩阵为例，storage 里依次是 `0,1,2,3,4,5`：

```
sizes      = (2, 3)
strides    = (3, 1)       # 行 stride=3（一行 3 个元素），列 stride=1
storage_offset = 0
```

那么元素 `[1][2]`（第 2 行第 3 列，即数字 5）的位置：

\[
\mathrm{offset} = 0 + 1 \cdot 3 + 2 \cdot 1 = 5
\]

正是 storage 下标 5。公式成立。

#### 4.3.2 核心流程

C++ 里 `TensorImpl` 把这三件套直接存成成员：

```cpp
  Storage storage_;
  ...
  c10::impl::SizesAndStrides sizes_and_strides_;
  int64_t storage_offset_ = 0;
  int64_t numel_ = 1;
```

参见 [c10/core/TensorImpl.h:2888](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorImpl.h#L2888)、[c10/core/TensorImpl.h:2923-2930](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorImpl.h#L2923-L2930)。注意几个细节：

- `storage_` 就是上一节讲的 `Storage`（`StorageImpl` 的引用计数包装）。
- `sizes_and_strides_` 是一个紧凑结构（`SizesAndStrides`），把 sizes 和 strides 打包在一起以节省内存。
- `storage_offset_` 默认 0，**单位是元素而非字节**。
- `numel_` 缓存了元素总数（`prod(sizes)`），避免每次重算。

读取它们的访问器都很短：

```cpp
  int64_t storage_offset() const {
    if (C10_UNLIKELY(matches_policy(SizesStridesPolicy::CustomSizes))) {
      return storage_offset_custom();
    }
    return storage_offset_;
  }
```

参见 [c10/core/TensorImpl.h:749-755](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorImpl.h#L749-L755)。正常情况下直接返回 `storage_offset_`；只有当张量是自定义子类（比如 nested tensor）时才走 `storage_offset_custom()`。`C10_UNLIKELY` 是个分支预测提示，告诉 CPU"这种情况很少见"，保证普通张量走快路径。

strides 的访问器结构一模一样：

```cpp
  IntArrayRef strides() const {
    if (C10_UNLIKELY(matches_policy(SizesStridesPolicy::CustomStrides))) {
      return strides_custom();
    }
    return sizes_and_strides_.strides_arrayref();
  }
```

参见 [c10/core/TensorImpl.h:783-788](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorImpl.h#L783-L788)。

#### 4.3.3 源码精读：offset 是怎么变成真实指针的

公式最终要落到一个内存地址上。`TensorImpl` 里把 `storage_offset_` 翻译成字节偏移的代码长这样（取 `data_type_.itemsize()` 乘以 offset）：

```cpp
    return data + data_type_.itemsize() * storage_offset_;
```

参见 [c10/core/TensorImpl.h:1723](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorImpl.h#L1723)。这是 raw 字节路径：拿 storage 的裸指针 `data`，加上 `元素大小 × storage_offset` 字节。

另一条类型化路径更直接（指针本身就是 typed `T*`，所以加 `storage_offset_` 即可，C++ 指针算术自动按 `sizeof(T)` 步进）：

```cpp
    return get_data() + storage_offset_;
```

参见 [c10/core/TensorImpl.h:1665](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorImpl.h#L1665)。两条路径殊途同归：**`storage_offset` 是元素单位，最终都要乘上 `itemsize` 才变成字节**。

把这条"下标 → 字节偏移"链路完整串起来就是：

1. 用户写下标 `t[i0, i1, ...]`。
2. 按 \(\sum i_d \cdot \mathrm{stride}_d\) 算出相对张量起点的元素偏移。
3. 加上 `storage_offset_`，得到相对 storage 起点的元素偏移。
4. 乘以 `data_type_.itemsize()`，得到字节偏移。
5. 加到 `storage_.data_ptr()` 上，得到最终内存地址。

而 `TensorImpl::storage()` 只是简单地把内部的 `storage_` 返回出来（带一个访问拦截）：

```cpp
  TENSORIMPL_MAYBE_VIRTUAL const Storage& storage() const {
    if (C10_UNLIKELY(storage_access_should_throw_)) {
      throw_storage_access_error();
    }
    return storage_;
  }
```

参见 [c10/core/TensorImpl.h:1073-1078](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorImpl.h#L1073-L1078)。Python 侧 `tensor.untyped_storage()` 最终就是从这里取数据。

#### 4.3.4 contiguous / view / reshape 与内存布局

理解了 stride/offset，下面几个高频 API 的行为就一目了然：

- **`contiguous()`**：如果张量已经是行优先连续（`is_contiguous()` 为真），直接返回自己；否则**真正拷贝一份数据**，让新张量的 strides 符合行优先规则。判断连续的核心是一个 `is_contiguous_` 位（见 [c10/core/TensorImpl.h:2951](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/c10/core/TensorImpl.h#L2951)）。
- **`view()`**：**不拷数据**，只换 `sizes`（并在可行时换 strides）。它要求张量 contiguous，否则会报错——因为非连续张量无法用一组新的 strides 表达成目标形状。
- **`reshape()`**：能 view 就 view，不能（非连续）就悄悄 contiguous + view，永远不报错。
- **转置 `t()` / `transpose()`**：**不拷数据**，只交换对应维度的 `sizes` 和 `strides`。这就是为什么转置几乎是零开销的。

一句话：凡是只动 `sizes/strides/storage_offset` 的操作都零拷贝；一旦现有布局无法表达目标形状（典型如对非连续张量 `view`、或扩容），就必须真正动数据。

#### 4.3.5 代码实践：手动推算转置张量的元素位置

这是本讲的主实践，对应规格里的实践任务。

实践目标：创建一个非连续张量（转置矩阵），打印 `stride` 与 `storage_offset`，用公式手动推算某元素位置，再用 `.contiguous()` 对比 stride 变化。

操作步骤：

```python
import torch

# 1. 行优先的 2x3 矩阵
a = torch.tensor([[1, 2, 3],
                  [4, 5, 6]], dtype=torch.int32)
print("a:")
print("  sizes   =", tuple(a.size()))        # (2, 3)
print("  strides =", a.stride())             # (3, 1)
print("  storage_offset =", a.storage_offset())  # 0
print("  is_contiguous  =", a.is_contiguous())   # True

# 2. 转置 -> 非连续
b = a.t()
print("\nb = a.t():")
print("  sizes   =", tuple(b.size()))        # (3, 2)
print("  strides =", b.stride())             # (1, 3)  ← 和 a 刚好交换
print("  storage_offset =", b.storage_offset())  # 0
print("  is_contiguous  =", b.is_contiguous())   # False
```

接下来用公式手动算 `b[1][0]`（即原矩阵 `a[0][1]`，值应该是 2）。代入：

\[
\mathrm{offset} = 0 + 1 \cdot \mathrm{stride}_0 + 0 \cdot \mathrm{stride}_1 = 1 \cdot 1 + 0 \cdot 3 = 1
\]

代码验证：

```python
# 3. 手动推算 b[1][0] 在 storage 里的元素下标
s0, s1 = b.stride()
off = b.storage_offset() + 1 * s0 + 0 * s1
print("\nb[1][0] 推算的 storage 元素下标 =", off)   # 预期 1
print("storage 里这个位置的值 =", b.untyped_storage()[off * 4 : off * 4 + 4])  # 字节视角
print("直接取 b[1][0] =", b[1][0].item())          # 预期 2
```

注意：`UntypedStorage` 是按**字节**索引的，int32 一个元素占 4 字节，所以读第 `off` 个元素要切片 `[off*4 : off*4+4]`。这正是 `StorageImpl::size_bytes_` 用字节、而 `storage_offset_` 用元素带来的"单位差"——一个常踩的坑。

最后对比 `contiguous()` 前后的 strides：

```python
# 4. contiguous 化，对比 stride 变化
c = b.contiguous()
print("\nc = b.contiguous():")
print("  strides =", c.stride())             # (2, 1)  ← 重新行优先
print("  is_contiguous  =", c.is_contiguous())   # True
print("  data_ptr 改变？", c.data_ptr() != b.data_ptr())  # True，因为发生了真实拷贝
```

需要观察的现象：

- `b` 的 strides 是 `(1, 3)`，和 `a` 的 `(3, 1)` 刚好交换，且 `data_ptr` 与 `a` 相同——转置零拷贝。
- 手算的元素下标 `1` 对应的值确实是 `2`，与直接索引 `b[1][0]` 一致。
- `contiguous()` 之后 strides 变回行优先的 `(2, 1)`，`data_ptr` 改变——这次是**真拷贝**了数据。

预期结果：完全印证「数据 vs 视图」分离设计，以及 stride/offset 公式。

如果手算时拿不准单位（字节还是元素），先记住：**`storage_offset` 和 `stride` 永远是元素单位，`nbytes` 和 `data_ptr` 偏移永远是字节单位**。

#### 4.3.6 小练习与答案

**练习 1**：给定 `x = torch.arange(12).reshape(3, 4)`，求 `x[2][1]` 在 storage 里的元素下标（不运行代码）。

参考答案：行优先，`strides = (4, 1)`，`storage_offset = 0`，所以 \(\mathrm{offset} = 0 + 2 \cdot 4 + 1 \cdot 1 = 9\)，即值 `9`。

**练习 2**：为什么对转置后的矩阵 `b = a.t()` 直接调用 `b.view(6)` 会报错，而 `b.reshape(6)` 不会？

参考答案：`view` 不允许拷贝，要求张量在当前 strides 下就能表达目标形状；非连续的 `b` 做不到，所以报错。`reshape` 在不能 view 时会退化为 `contiguous().view()`，允许拷贝，因此总能成功。

**练习 3**：`tensor.untyped_storage().data_ptr()` 和 `tensor.data_ptr()` 在数值上一定相等吗？什么情况下不相等？

参考答案：不一定相等。`tensor.data_ptr()` 返回的是"张量第 0 个元素"的地址，即 `storage.data_ptr() + storage_offset * itemsize`。只有当 `storage_offset == 0` 时两者才相等；切片、某些 view 出来的张量 `storage_offset > 0`，此时 `tensor.data_ptr()` 会更大。

## 5. 综合实践

设计一个小任务把本讲三块内容串起来：**用 stride/offset 知识，手动复现一个"零拷贝转置 + 切片"操作的效果**。

任务描述：

1. 创建 `x = torch.arange(20).reshape(4, 5)`（int64）。
2. 取它的转置 `xt = x.t()`，再取后两行 `y = xt[1:3]`。
3. 不直接索引 `y`，而是仅用 `x.untyped_storage()`、`y.stride()`、`y.storage_offset()` 和 `x.element_size()`，写一个函数 `manual_get(row, col)`，返回 `y[row][col]` 的值。
4. 用 `y[row][col].item()` 校验你的函数至少在 3 个不同 `(row, col)` 上正确。

提示：

- 先算 `y.storage_offset()`（它已经在转置 + 切片后被叠加好）。
- 再用 `offset = storage_offset + row*stride0 + col*stride1`。
- int64 是 8 字节，用 Python `int.from_bytes` 从 storage 字节切片里读出。
- 如果运行环境拿不到 storage 字节内容，可降级为"只算出元素下标并与 `y` flatten 后对照"，并在报告里标注「待本地验证字节读取部分」。

这个练习同时考验你：理解 Python Storage 封装（4.1）、C++ offset 公式（4.2/4.3）、以及 contiguous 与否的判别（4.3.4）。

## 6. 本讲小结

- **Tensor = 数据 + 视图**：数据由 `Storage`（C++ `StorageImpl`）持有，视图由 `TensorImpl` 的 `sizes_and_strides_` 和 `storage_offset_` 描述。
- **Storage 在 Python 里有两个类**：推荐用的 `UntypedStorage`（不带 dtype）和已废弃的 `TypedStorage`（只是 `UntypedStorage` + dtype 的壳）。取底层用 `tensor.untyped_storage()`。
- **`StorageImpl` 真正存的是 `data_ptr_`（内存句柄）和 `size_bytes_`（字节数）**，外加分配器、引用计数、错误处理开关等；它**不知道形状语义**。
- **定位元素靠公式** \(\mathrm{offset} = \mathrm{storage\_offset} + \sum i_d \cdot \mathrm{stride}_d\)，单位是元素；再乘 `itemsize` 才是字节。
- **`storage_offset` 和 `stride` 是元素单位，`nbytes` 和 `data_ptr` 偏移是字节单位**——这是最常踩的单位坑。
- **只改 sizes/strides/offset 的操作（转置、切片、view）零拷贝**；一旦现有布局表达不了目标形状（非连续 view、扩容），就必须真正拷贝数据（`contiguous()`、`reshape()` 的退化路径）。

## 7. 下一步学习建议

- **继续往 C++ 钻**：本讲的 `TensorImpl` 只是冰山一角，下一阶段（u3-l4）会系统讲 `TensorImpl.h` 的完整字段、它如何与 `DispatchKeySet` 协作、以及 sizes/strides 的紧凑存储优化。
- **衔接 dtype/device/layout**：本讲多次出现 `itemsize`、`data_type_`，这正是 u2-l3（dtype/device/layout 与 TensorOptions）的主题，建议接着读 `torch/types.py` 与 `c10/core/TensorOptions.cpp`。
- **看一次真实算子调用**：有了"数据 + 视图"的模型，再去读 u2-l4（算子调用路径与 _C 绑定），就能理解一个 `torch.add` 是如何在同一个 storage 上原地或非原地写回数据的。
- **进阶主题**：当你以后读到 `torch.compile`、FakeTensor、CUDA caching allocator 时，会发现它们全都建立在本讲的「Storage/offset/stride」模型之上——FakeTensor 之所以"没有数据"，正是通过本讲看到的 `throw_on_immutable_data_ptr_` 等开关实现的。
