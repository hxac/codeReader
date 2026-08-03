# mem\* 框架：构建块与架构特化

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `block` / `tail` / `head_tail` / `loop_and_tail` 四个**构建块（building block）**各自做什么，并理解它们之间「层层组合」的关系。
- 解释 `builtin` / `generic` / **架构特化（arch）** 三类**作用域（scope）**的分工，以及它们在「可移植性」与「性能」之间的取舍。
- 顺着 `memcpy` 入口点，看清它是如何**按尺寸（count）分派**到不同构建块的。
- 理解 `head_tail` 为什么会重复写入首尾字节（重叠写），以及这样做为什么是安全的、又是为了换取什么。

本讲是字符/内存函数族的进阶篇。上一篇 `u5-l1` 讲的是「最薄」的入口点（`isalpha` 只做边界检查再委托）；本讲则相反——`memcpy`/`memset` 这类函数是整个 libc 里**实现最重、优化最深**的部分，而 LLVM-libc 用一套精巧的框架把它们统一管理起来。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **入口点（entrypoint）机制**（`u2-l1`）：知道每个公开函数是一个独立的构建单元，入口实现包在 `LIBC_NAMESPACE_DECL` 命名空间里、用 `LLVM_LIBC_FUNCTION` 宏暴露成 C 符号。
- **`__support` 内部库**（`u4-l1`）：知道真正「干活」的算法会下沉到 `src/__support/`，入口点只是一层薄壳。本讲的 `src/string/memory_utils/` 正是这种下沉的典型代表。
- **C++ 模板与 `if constexpr`**：框架大量用模板参数（如 `<Size>`）在编译期生成不同宽度的内存操作，并用 `if constexpr` 在编译期裁剪分支。
- 一点点**汇编直觉**：知道「一次写 8 字节比写 8 次 1 字节快」「循环有开销」即可，不需要会写汇编。

一个关键直觉先放在前面：`memcpy` 之所以慢或快，几乎只取决于**「每次处理多少字节（宽度）」**和**「怎么处理尺寸不是宽度整数倍的多余部分」**。本讲的整个框架，就是围绕这两个问题展开的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/string/memory_utils/README.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/README.md) | 框架的「说明书」，用 `Memset` 例子讲清构建块与作用域的设计思想。本讲最重要的参考。 |
| [src/string/memcpy.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memcpy.cpp) | `memcpy` 入口点：做空指针检查后委托给 `inline_memcpy`。 |
| [src/string/memory_utils/inline_memcpy.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/inline_memcpy.h) | **分发层**：按编译开关和目标架构，把 `inline_memcpy` 宏指向某个具体实现。 |
| [src/string/memory_utils/op_builtin.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_builtin.h) | **builtin 作用域**：用 Clang 的 `__builtin_memcpy_inline` / `__builtin_memset_inline` 实现构建块。 |
| [src/string/memory_utils/op_generic.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_generic.h) | **generic 作用域**：纯 C++（整数类型 + 向量扩展）实现的构建块，含 `Memset`/`Memmove`/`Memcmp`/`Bcmp`。 |
| [src/string/memory_utils/op_x86.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_x86.h) | **x86 架构特化作用域**：提供 `rep;movsb` 等整段操作，并为 SIMD 类型补齐 `cmp`/`eq` 特化。 |
| [src/string/memory_utils/x86_64/inline_memcpy.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/x86_64/inline_memcpy.h) | **x86_64 尺寸分派实现**：按 `count` 选择不同宽度的构建块，并在大尺寸时进入循环或 `rep;movsb`。 |
| [src/string/memory_utils/utils.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/utils.h) | 底层小工具：`Ptr`/`CPtr` 类型别名、定长拷贝 `memcpy_inline`、对齐计算等。 |

> 小提示：`memory_utils/` 还包含 `aarch64/`、`arm/`、`riscv/` 等子目录，结构完全对称（每个架构一个 `inline_memcpy.h`）。本讲以 x86_64 为主线，其它架构是同样的套路。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**构建块 → 作用域 → 尺寸分派 → 架构特化**。这四者正好对应框架从「最底层原子操作」一路堆到「针对某 CPU 的最终实现」的全过程。

### 4.1 构建块（building blocks）：从 block 到 loop_and_tail

#### 4.1.1 概念说明

`memcpy`/`memset`/`memmove`/`memcmp`/`bcmp`/`bzero` 这六个函数，看起来各不相同，但它们都可以用同一组**更底层的操作**拼出来。README 把这组底层操作定义成四个「构建块」：

- **`block`**：在指针起点处理一段固定 `SIZE` 字节。
- **`tail`**：处理缓冲区的**最后** `SIZE` 字节，即区间 `[dst + count - SIZE, dst + count]`。
- **`head_tail`**：处理最前面和最后面各 `SIZE` 字节。等价于先调一次 `block` 再调一次 `tail`。
- **`loop_and_tail`**：在一个循环里反复调 `block`，尽量吃掉 `count` 字节，剩下的零头用一次 `tail` 收尾。

这四个构建块是**层层向上组合**的：`tail` 依赖 `block`，`head_tail` 依赖 `block` + `tail`，`loop_and_tail` 依赖 `block` + `tail`。**真正需要「针对某种操作（拷贝/填充/比较）各自实现」的，只有 `block` 一个**。这是整个框架最关键的一句话。

#### 4.1.2 核心流程

为什么这样设计？因为只要给定了 `block`，处理**任意尺寸** `count` 的策略就被统一了：

```text
count == 0        → 直接返回
count ∈ [1, SIZE] → block（或更小宽度的 block）
count ∈ (SIZE, 2·SIZE] → head_tail（写头部 SIZE + 写尾部 SIZE，中间有重叠）
count > 2·SIZE    → loop_and_tail（循环 block + 尾部 tail）
```

用一个 `SIZE=16` 的 `Memset` 举例，README 给出了最直白的演示（注意第 27、28 行的注释：`0 到 4 字节会被写两次`）：

```C++
extern "C" void memset(const char* dst, int value, size_t count) {
   if (count == 0) return;
   if (count == 1) return Memset<1>::block(dst, value);
   if (count == 2) return Memset<2>::block(dst, value);
   if (count == 3) return Memset<3>::block(dst, value);
   if (count <= 8) return Memset<4>::head_tail(dst, value, count);  // 0 到 4 字节写两次
   if (count <= 16) return Memset<8>::head_tail(dst, value, count); // 同理
   return Memset<16>::loop_and_tail(dst, value, count);
}
```

对应的 `Memset<Size>` 结构体里，`tail`/`head_tail`/`loop_and_tail` 全部用 `block` 表达：

```C++
template <size_t Size>
struct Memset {
  static constexpr size_t SIZE = Size;

  LIBC_INLINE static void block(Ptr dst, uint8_t value) { /* 真正实现这里 */ }

  LIBC_INLINE static void tail(Ptr dst, uint8_t value, size_t count) {
    block(dst + count - SIZE, value);            // 跳到尾部再 block
  }

  LIBC_INLINE static void head_tail(Ptr dst, uint8_t value, size_t count) {
    block(dst, value);                            // 头
    tail(dst, value, count);                      // 尾
  }

  LIBC_INLINE static void loop_and_tail(Ptr dst, uint8_t value, size_t count) {
    size_t offset = 0;
    do {
      block(dst + offset, value);
      offset += SIZE;
    } while (offset < count - SIZE);
    tail(dst, value, count);                      // 收尾零头
  }
};
```

README 在这段代码后面点破了设计意图：`tail`/`head_tail`/`loop_and_tail` 都是建立在 `block` 之上的**高阶函数**，只有 `block` 必须真正实现。早期版本曾用「模板函数」来做这种组合，后来发现**把实现显式写出来更易读**。

更重要的是接下来那句：**这种设计提供了「定制点（customization points）」**。因为每个构建块都是显式命名的独立函数，某个架构若能对其中某一个做得更好，就可以**只覆盖那一个**。README 给的例子是 aarch64 上的 `bcmp`：它的 `head_tail` 可以用向量归约（vector reduction）指令实现得更好，那就只重写 `head_tail`，其余照旧复用 `block`。

#### 4.1.3 源码精读

README 是这套思想的权威说明，本节引用的三处一定要亲自打开看一遍：

1. 构建块的定义（四个 bullet）：
   [src/string/memory_utils/README.md:11-17](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/README.md#L11-L17)
   —— 官方对 `block`/`tail`/`head_tail`/`loop_and_tail` 各自处理区间的精确文字定义。

2. 「只有 `block` 真正需要实现」与定制点：
   [src/string/memory_utils/README.md:64-66](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/README.md#L64-L66)
   —— 这三行是整个框架哲学的浓缩，点明「显式实现更可读」和「提供定制点」两个动机。

3. `Memset` 结构体的真实写法（与上面伪代码一致）：
   [src/string/memory_utils/README.md:36-62](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/README.md#L36-L62)
   —— 这是本讲「代码实践」的模板来源。

#### 4.1.4 代码实践

> **实践目标**：亲手把 README 里 `Memset` 的写法迁移成 `Memcpy`，体感「只实现 `block`，其余自动组合」。

操作步骤（纯源码阅读型，无需编译）：

1. 打开 [src/string/memory_utils/op_builtin.h:28-65](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_builtin.h#L28-L65) 里的 `builtin::Memcpy<Size>`，对照 README 的 `Memset`，把每个方法一一对应。
2. 在一张纸上仿照 README 的 `Memset` 结构体，为 `Memcpy<Size>` 写出 `block`/`tail`/`head_tail`/`loop_and_tail` 四个方法骨架（注意 `Memcpy` 有**源指针 `src` 和目标指针 `dst` 两个**，而 `Memset` 只有一个 `dst` + 一个 `value`）。
3. 思考并在旁边写下一句话：`head_tail` 为什么会**重复写首尾字节**？（提示：见 4.1.5 与 4.3.4）

需要观察的现象：你会发现除了多一个 `src` 参数、且 `block` 调用的是「拷贝」而非「填充」之外，四个方法的**形状和 README 的 `Memset` 完全一样**。这正是框架的威力——换一种操作，骨架不变。

预期结果：你写出的骨架应与 `builtin::Memcpy` 的 `tail`/`head_tail`/`loop_and_tail` 一致；真实的 `builtin::Memcpy` 还额外提供了一个带 `offset` 参数的 `block_offset` 版本（见 [src/string/memory_utils/op_builtin.h:30-33](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_builtin.h#L30-L33)），用来给编译器更稳定的寻址提示，但思路完全相同。

#### 4.1.5 小练习与答案

**练习 1**：README 的 `loop_and_tail` 里，循环条件是 `offset < count - SIZE`，循环体结束后又调一次 `tail`。为什么不直接写成 `while (offset < count)`？

**答案**：因为 `count` 通常不是 `SIZE` 的整数倍。如果用 `offset < count`，最后一段不足 `SIZE` 的零头要么得单独做变宽处理、要么越界。改用 `offset < count - SIZE` 后，循环保证「每次都写满一个完整的 `SIZE` 块、且不写出界」，剩下的 `[count-SIZE, count)` 这段尾巴交给 `tail`——它天然就是一次定宽 `block`，落在合法区间内。代价是最后一次 `block` 与 `tail` 可能有重叠（见 4.3.4），但这对 `memset`/`memcpy` 是无害的。

**练习 2**：有人说「既然只有 `block` 要实现，那为什么不把 `tail`/`head_tail`/`loop_and_tail` 写成模板函数复用？」请用 README 的理由反驳。

**答案**：README 明确说，早期确实用模板函数做组合，但「显式写出实现更可读」；更重要的是显式版本**提供了定制点**——某个架构可以只重写 `head_tail`（如 aarch64 的 `bcmp` 用向量归约）而不动其它部分，模板函数复用做不到这种「逐函数」级别的覆盖。

### 4.2 作用域（scopes）：builtin / generic / arch

#### 4.2.1 概念说明

「构建块」回答了「用什么形状的函数拼」，但没回答「`block` 里面到底怎么搬字节」。同一个 `Memset<16>::block`，在一台有 AVX 的 x86 上和在一颗小 RISC-V 核上，最优实现完全不同。框架用**作用域（scope）**来组织这些「同一构建块的不同实现」。

README 把作用域分成三类（它的措辞是 "scoped specializations"）：

- **`builtin` 作用域**：把 `block` 的实现**交给编译器**。依靠 Clang 提供的保证内联的内建函数，如 `__builtin_memset_inline`、`__builtin_memcpy_inline`。理想情况下，编译器最懂目标平台，由它生成 `block` 的代码最好。
- **`generic` 作用域**：用**纯 C++** 写 `block`——靠原生整数类型（`uint16_t`/`uint32_t`/`uint64_t`）和 Clang 的向量扩展（`__attribute__((__vector_size__(16)))` 等）来搬运字节。不依赖任何内建函数，可移植到任何能编译 C++ 的平台。
- **架构特化作用域**（如 `x86`、`aarch64`）：用**特定架构/微架构**的特性，如 x86 的 `rep;movsb`、aarch64 的 `dc zva`。README 的原则是「**尽量用 builtin，万不得已才 `asm volatile`**」。

三者不是互斥的——同一次调用可以「小尺寸用 `generic`、大尺寸循环用 `x86`」，README 给的正是这种混用范例。

#### 4.2.2 核心流程

README 用一段「混用」代码说明作用域如何协作：小尺寸走 `generic`（可移植、够用），循环走 `x86`（针对架构优化）：

```C++
extern "C" void memset(const char* dst, int value, size_t count) {
   if (count == 0) return;
   if (count == 1) return generic::Memset<1>::block(dst, value);
   if (count == 2) return generic::Memset<2>::block(dst, value);
   if (count == 3) return generic::Memset<3>::block(dst, value);
   if (count <= 8) return generic::Memset<4>::head_tail(dst, value, count);
   if (count <= 16) return generic::Memset<8>::head_tail(dst, value, count);
   return x86::Memset<16>::loop_and_tail(dst, value, count);   // 循环交给 x86
}
```

三类作用域在「可移植性 ↔ 性能」轴上的位置可记成下表：

| 作用域 | 实现手段 | 可移植性 | 性能 | 典型用途 |
| --- | --- | --- | --- | --- |
| `builtin` | `__builtin_*_inline`（仅 Clang） | 中（需 Clang） | 高（编译器优化） | `memcpy` 的主力 `block` |
| `generic` | 纯 C++ 整数 / 向量扩展 | 高 | 中高 | 小尺寸、无 intrinsic 时的回退 |
| arch（x86/aarch64…） | intrinsic 或 `asm volatile` | 低（仅该架构） | 最高 | 大循环、整段操作（`rep;movsb` 等） |

> 一个容易忽略的细节：`op_generic.h` 里有 `generic::Memset`、`generic::Memmove`、`generic::Memcmp`、`generic::Bcmp`，**却没有 `generic::Memcpy`**。原因是 `memcpy` 的 `block` 用编译器内建（`__builtin_memcpy_inline`）几乎总是最优，所以「拷贝」这一类操作直接落在 `builtin::Memcpy` 上，不再单独写一份 generic 版。这一点你会在 4.2.3 的源码里得到印证。

#### 4.2.3 源码精读

- `builtin` 作用域的 `Memcpy`：[src/string/memory_utils/op_builtin.h:28-65](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_builtin.h#L28-L65)
  —— `block_offset`（第 30–33 行）调用的 `memcpy_inline<Size>` 最终落到 `__builtin_memcpy_inline`；`tail`/`head_tail`/`loop_and_tail` 全部用它组合出来。这是 4.1 练习里 `Memcpy` 骨架的真实参考答案。

- `generic` 作用域的 `Memset`（用整数/向量 `splat`+`store` 实现 `block`）：
  [src/string/memory_utils/op_generic.h:166-204](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_generic.h#L166-L204)
  —— 注意它的 `block`（第 170–180 行）按元素类型分三种情况：标量、向量、数组，分别用 `splat` 把 `value` 广播再 `store`。这里能清楚看到 generic 不依赖任何 intrinsic。

- `memcpy_inline` 是 builtin 作用域的最终落脚点：
  [src/string/memory_utils/utils.h:84-108](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/utils.h#L84-L108)
  —— 有 `__builtin_memcpy_inline` 就用它；否则退化成逐字节循环（第 102–103 行），保证在无该内建的编译器上仍可编译。

- README 对三类作用域的原文定义：
  [src/string/memory_utils/README.md:85-97](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/README.md#L85-L97)
  —— builtin「靠编译器」、generic「纯 C++」、arch「`rep;movsb`/`dc zva`，尽量 builtin、最后才 asm」。

#### 4.2.4 代码实践

> **实践目标**：确认「同一个构建块签名，在不同作用域里有不同实现」。

操作步骤：

1. 打开 `builtin::Memcpy::block_offset`：[src/string/memory_utils/op_builtin.h:30-33](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_builtin.h#L30-L33)，记下它调用 `memcpy_inline<Size>`。
2. 打开 `generic::Memset::block`：[src/string/memory_utils/op_generic.h:170-180](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_generic.h#L170-L180)，记下它调用 `splat` + `store`。
3. 对比两者的方法签名：都是 `static void block(...)`、都带 `SIZE` 常量，但函数体一个交给内建、一个交给纯 C++。

需要观察的现象：**接口相同、实现不同**——这正是「作用域」的本质：上层（尺寸分派）只看接口（`block`/`head_tail`/`loop_and_tail`），不关心下层用哪种手段实现。

预期结果：你能用一句话概括「作用域 = 同一构建块接口的一组可替换实现」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 README 说架构特化作用域要「尽量用 builtin，最后才 `asm volatile`」？

**答案**：内建函数（builtin）让编译器保有完整可见性——它能做寄存器分配、调度、与周围代码合并优化；而 `asm volatile` 是一堵「黑墙」，编译器无法看穿、无法重排，只应留到没有对应 builtin 的指令（如 `rep;movsb`）时才用。所以原则是：能 builtin 就 builtin，万不得已才 asm。

**练习 2**：`generic::Memset::block` 为什么要区分「标量 / 向量 / 数组」三种元素类型？

**答案**：因为请求的 `SIZE` 可能大于平台最宽的单个原生类型。比如平台最宽是 `uint64_t`（8 字节），要填 32 字节时，generic 会把它当成 `cpp::array<uint64_t, 4>`（数组情况）循环处理；若平台有 AVX，则 32 字节可作为一个 `generic_v256` 向量（向量情况）一次搞定；8 字节以内则走标量。三分支让同一套代码自适应「有没有向量支持」。

### 4.3 尺寸分派：入口点如何按 count 选择构建块

#### 4.3.1 概念说明

有了「构建块（形状）」和「作用域（实现）」，还缺最后一层：**给一个具体的 `count`，到底调哪个构建块、用多大的 `SIZE`？** 这就是「尺寸分派」要解决的问题，由入口点 + 分发层共同完成。

LLVM-libc 的 `memcpy` 走的是一条**两级分派**链路：

1. **编译期分派**：在 [inline_memcpy.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/inline_memcpy.h) 里，用一连串 `#if` 按编译开关和目标架构，把宏 `LIBC_SRC_STRING_MEMORY_UTILS_MEMCPY` 指向某个具体函数（如 `inline_memcpy_x86_maybe_interpose_repmovsb`、`inline_memcpy_builtin`、`inline_memcpy_byte_per_byte`）。
2. **运行期分派**：被选中的那个函数（如 x86_64 的 `inline_memcpy_x86`）内部再按 `count` 的大小，逐级选择不同 `SIZE` 的构建块。

#### 4.3.2 核心流程

先看最外层的入口点 `memcpy.cpp`——它非常薄，与 `isalpha` 一样只做防御性检查，然后委托：

```text
memcpy(dst, src, size)
  ├─ if (size) { 空指针检查 dst / src }
  └─ inline_memcpy(dst, src, size)
        └─ （编译期被替换成某个具体实现）
```

`inline_memcpy` 本身只有一行——调用宏指向的实现。但它带着 `[[gnu::flatten]]` 属性，意思是「把这个函数体**拍平**进调用方」，于是分派逻辑在最终生成的 `memcpy` 符号里是直接内联的，没有额外调用开销。

进入被选中的实现后（以 x86_64 为例），就是一串**按 `count` 从小到大的 `if`**：

```text
count == 0/1/2/3/4  → builtin::Memcpy<N>::block
count < 8           → builtin::Memcpy<4>::head_tail
count < 16 (视向量宽度) → builtin::Memcpy<8>::head_tail
count < 32 / < 64   → builtin::Memcpy<16>/<32>::head_tail
count 更大          → loop_and_tail（或 rep;movsb，见 4.4）
```

注意这条链的形状与 4.1.2 里 README 的 `memset` 例子**完全同构**：都是「极小尺寸直接 block、中等尺寸 head_tail、大尺寸 loop_and_tail」。差别只在于：x86_64 实现把每一步的 `SIZE` 与 CPU 支持的向量宽度（SSE2=16 / AVX=32 / AVX-512=64）挂钩。

#### 4.3.3 源码精读

- 入口点的薄壳：[src/string/memcpy.cpp:17-26](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memcpy.cpp#L17-L26)
  —— 标准 `LLVM_LIBC_FUNCTION` 定义；`size` 非零时做 `LIBC_CRASH_ON_NULLPTR`，再调 `inline_memcpy`。

- 编译期分派的全部层级：
  [src/string/memory_utils/inline_memcpy.h:18-43](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/inline_memcpy.h#L18-L43)
  —— 优先级一目了然：`LIBC_COPT_USE_MEM_BUILTINS`（强制 builtin）→ `LIBC_COPT_MEMCPY_USE_EMBEDDED_TINY`（极小代码体积，逐字节）→ 按架构（x86/arm/aarch64/riscv）→ GPU/WASM（builtin）→ 最终回退到逐字节。每一支都把宏设成对应函数名。

- 拍平的分派入口：
  [src/string/memory_utils/inline_memcpy.h:47-51](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/inline_memcpy.h#L47-L51)
  —— `[[gnu::flatten]]` + 调用宏指向的函数。

- x86_64 的运行期尺寸分派（核心）：
  [src/string/memory_utils/x86_64/inline_memcpy.h:204-250](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/x86_64/inline_memcpy.h#L204-L250)
  —— 第 206–209 行先按 AVX-512/AVX/SSE2 选 `VECTOR_SIZE`；第 210–236 行就是那一串「按 count 选不同 SIZE 的 head_tail」；更大的尺寸在第 237–249 行交给 `loop_and_tail` 或带软件预取的版本。

- 最终回退（逐字节）的样子：
  [src/string/memory_utils/generic/byte_per_byte.h:23-29](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/generic/byte_per_byte.h#L23-L29)
  —— 当架构既不在已知列表里、又没开 builtin 时，`memcpy` 退化成这个 `for` 循环；它强调「最小代码体积」，需配 `-Os`/`-Oz` 编译。

#### 4.3.4 代码实践

> **实践目标**：跟随一次 `memcpy(dst, src, 6)` 调用，走通「入口 → 分发 → 构建块」全链路（源码阅读型，假设 x86_64、默认配置）。

操作步骤：

1. 从 [src/string/memcpy.cpp:24](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memcpy.cpp#L24) 的 `inline_memcpy(dst, src, size)` 进入。
2. 在 [inline_memcpy.h:24-27](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/inline_memcpy.h#L24-L27) 确认：x86_64 命中 `LIBC_TARGET_ARCH_IS_X86` 分支，宏被设成 `inline_memcpy_x86_maybe_interpose_repmovsb`。
3. 跳到 [x86_64/inline_memcpy.h:252-265](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/x86_64/inline_memcpy.h#L252-L265)：默认 `K_REP_MOVSB_THRESHOLD == SIZE_MAX`（不使用 rep;movsb），于是转进 `inline_memcpy_x86`。
4. 在 [x86_64/inline_memcpy.h:210-221](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/x86_64/inline_memcpy.h#L210-L221) 里，`count==6` 不命中前几个相等分支，但满足 `count < 8`，于是走 `builtin::Memcpy<4>::head_tail(dst, src, count)`。
5. 最后在 [op_builtin.h:44-48](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_builtin.h#L44-L48) 展开 `head_tail`：先 `block(dst, src)`（写前 4 字节），再 `tail(dst, src, count=6)` 即 `block_offset(dst, src, 6-4=2)`（写最后 4 字节，区间 `[2,6)`）。

需要观察的现象：第 5 步里，头部写 `[0,4)`、尾部写 `[2,6)`，**区间 `[2,4)` 这 2 个字节被写了两次**。这就是 `head_tail` 的「重叠写」。

预期结果（`memcpy(dst, src, 6)` 的内存效果，假设 `src` 与 `dst` 不重叠）：

```text
下标:  0  1  2  3  4  5
head: [S0 S1 S2 S3]            ← block，写 [0,4)
tail:       [S2 S3 S4 S5]      ← tail=block_offset(2)，写 [2,6)
结果: [S0 S1 S2 S3 S4 S5]      ← 中间 S2 S3 被写两次，但值相同，无害
```

> 这一步若想在本机确认，可临时给 `memcpy.cpp` 加一条 `__builtin_printf` 打印 `size`（仅用于学习，勿提交），观察不同 `size` 走到的分支——属于「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `inline_memcpy` 要标 `[[gnu::flatten]]`？

**答案**：分发层只是「选择并调用」某个具体实现，本身不做实事。若不拍平，每次 `memcpy` 调用都会多一层函数调用（及其带来的寄存器保存/恢复、跳转）。`flatten` 把分发逻辑内联进 `memcpy` 符号本身，使最终生成代码等价于「直接走到被选中的那个实现」，消除分发开销。

**练习 2**：在默认 x86_64 配置下，`memcpy` 处理 `count=100` 会走到哪个构建块？

**答案**：`count=100` 大于 64，不会命中前面的 `head_tail` 分支，而是落到 [x86_64/inline_memcpy.h:237-249](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/x86_64/inline_memcpy.h#L237-L249) 的大尺寸分支——有 AVX 时走 `inline_memcpy_x86_avx_ge64`（内部先 `head_tail` 对齐，再 `builtin::Memcpy<64>::loop_and_tail`），否则走 SSE2 版本。两条路最终都用 `loop_and_tail` 反复搬 64（或 32）字节。

### 4.4 架构特化：rep;movsb、SIMD 与可移植性的取舍

#### 4.4.1 概念说明

尺寸分派解决了「用多大宽度」，但当 `count` 非常大时，「循环搬 64 字节」未必是最优解——某些 CPU 提供了**整段搬运指令**，可以一口气搬完一大块。x86 上最著名的就是 `rep;movsb`。架构特化作用域就是为这类「整段操作」准备的。

`rep;movsb` 的取舍很有意思，它体现了硬件演进带来的反转：

- **早期**：`rep;movsb` 比「手动用 SSE/AVX 循环」慢得多，大家尽量避开。
- **后来（ERMS / FSRM）**：Intel/AMD 优化了 `rep;movsb` 的微架构实现，对**大块**拷贝它反而更优（且代码体积小、对缓存友好）。

所以 LLVM-libc 没有简单地「用」或「不用」`rep;movsb`，而是把它做成一个**可配置的阈值**：小于阈值走 SIMD 循环，大于阈值走 `rep;movsb`，由编译开关 `LIBC_COPT_MEMCPY_X86_USE_REPMOVSB_FROM_SIZE` 决定。

与此同时，架构特化作用域还承担**为 SIMD 类型补全比较原语**的职责：`memcmp`/`bcmp` 要用 128/256/512 位向量做按字节比较，需要 `_mm_movemask_epi8`、`_mm256_cmpeq_epi8` 等 intrinsic，这些都集中在 `op_x86.h` 里。

#### 4.4.2 核心流程

x86_64 的 `memcpy` 顶层函数被命名为 `inline_memcpy_x86_maybe_interpose_repmovsb`——「maybe interpose」点明了它的逻辑：

```text
默认 K_REP_MOVSB_THRESHOLD == SIZE_MAX（=不使用）
  → 直接走 inline_memcpy_x86（纯 SIMD 分派）

若把阈值配成 0（=完全使用）
  → 所有尺寸都走 x86::Memcpy::repmovsb

若把阈值配成某个数 T（=按需切换）
  → count >= T 走 repmovsb；否则走 inline_memcpy_x86
```

`x86::Memcpy::repmovsb` 的本体就是一行内联汇编（MSVC 下用 `__movsb`）：

```C++
asm volatile("rep movsb" : "+D"(dst), "+S"(src), "+c"(count) : : "memory");
```

`"+D"`/`"+S"`/`"+c"` 分别绑定 `rdi`/`rsi`/`rcx`（x86 的字符串操作三件套寄存器），`"memory"` 是内存屏障，告诉编译器这段汇编读写内存、不得乱序。

#### 4.4.3 源码精读

- `rep;movsb` 的实现：
  [src/string/memory_utils/op_x86.h:63-72](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_x86.h#L63-L72)
  —— 注意它**不是构建块结构体**，而是 `x86::Memcpy` 里一个「整段操作」方法 `repmovsb(dst, src, count)`，签名与 `block` 不同（直接吃整个 count）。

- 阈值「按需切换」的分派：
  [src/string/memory_utils/x86_64/inline_memcpy.h:252-265](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/x86_64/inline_memcpy.h#L252-L265)
  —— 三个 `if constexpr` 分支对应「全用 / 全不用 / 按阈值用」，是 `[[gnu::flatten]]` 之后的真正顶层。

- 阈值与缓存常量的定义：
  [src/string/memory_utils/x86_64/inline_memcpy.h:50-54](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/x86_64/inline_memcpy.h#L50-L54)
  —— `LIBC_COPT_MEMCPY_X86_USE_REPMOVSB_FROM_SIZE` 默认 `SIZE_MAX`（即默认不启用 rep;movsb）。

- SIMD 比较原语（架构特化为 generic 补的料）：
  [src/string/memory_utils/op_x86.h:166-202](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_x86.h#L166-L202)
  —— 这里为 `__m128i` 特化了 `eq`/`neq`/`cmp_neq`，用 `_mm_xor_si128`+`_mm_testz_si256` 判等、`_mm_max_epu8`+`movemask` 做字典序比较。它们会被 `generic::Memcmp`/`generic::Bcmp` 的模板查到（依赖 ADL/特化），从而让「比较」类函数自动获得 SIMD 加速。

- CMake 注册（确认这些都是头文件库，不产生公开符号）：
  [src/string/memory_utils/CMakeLists.txt:1-47](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/CMakeLists.txt#L1-L47)
  —— `memory_utils` 用 `add_header_library` 声明，`HDRS` 把所有 `op_*.h` 和各架构的 `inline_*.h` 列进去，`DEPENDS` 指向 `__support` 下的 `common`/`CPP.*` 等内部目标。这呼应 `u4-l1`：`memory_utils` 是 `__support` 性质的内部能力，由入口点经 `DEPENDS` 引用。

#### 4.4.4 代码实践

> **实践目标**：理解 `rep;movsb` 是「可选的整段加速」，并学会如何开启它。

操作步骤：

1. 阅读 [src/string/memory_utils/op_x86.h:64-71](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_x86.h#L64-L71)，确认 `repmovsb` 只有一行汇编，且它**一次性吃掉整个 `count`**，与构建块「分块」的思路完全不同。
2. 阅读 [src/string/memory_utils/x86_64/inline_memcpy.h:252-265](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/x86_64/inline_memcpy.h#L252-L265)，对照三段 `if constexpr`，写出「阈值 == 0 / == SIZE_MAX / == T」三种配置下的行为。
3. （可选，待本地验证）若你有可运行的 runtimes 构建（见 `u1-l3`），在 CMake 配置时加 `-DLIBC_COPT_MEMCPY_X86_USE_REPMOVSB_FROM_SIZE=4096`，重新构建后用 `benchmarks`（见 `u10-l3`）测量大块 `memcpy` 的耗时变化。

需要观察的现象：开启阈值后，**大于阈值的拷贝**会改走 `rep;movsb`，而**小于阈值的拷贝**仍走 SIMD 分派——两者共存，按 `count` 自动切换。

预期结果：你能在一张表里写出三种阈值配置的行为；并理解「为什么默认是 `SIZE_MAX`（关）」——因为是否受益高度依赖具体 CPU 微架构，把选择权留给构建者更安全。

#### 4.4.5 小练习与答案

**练习 1**：`x86::Memcpy::repmovsb` 为什么不在 `builtin` 或 `generic` 作用域里？

**答案**：`rep;movsb` 是 x86 专有的字符串搬运指令，既没有跨平台的 Clang builtin（所以不在 builtin 作用域），也不能用纯 C++ 整数/向量操作表达（所以不在 generic 作用域）。它必须用 `asm volatile`（或 MSVC 的 `__movsb`）发出这条特定机器指令，因而只能落在架构特化的 `x86` 作用域里。

**练习 2**：`op_x86.h` 里为 `__m128i` 特化 `eq`/`cmp_neq`，但 `generic::Memcmp` 的模板声明在 `op_generic.h`。这两者是怎么「接上线」的？

**答案**：`generic::Memcmp` 调用的是无约束的函数模板 `eq<T>`/`cmp_neq<T>`；`op_x86.h` 在**同一个 `LIBC_NAMESPACE_DECL::generic` 命名空间**里对 `T = __m128i` 做了显式特化（见 [op_x86.h:166-202](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_x86.h#L166-L202)）。因为特化与主模板同命名空间，重载决议会精确命中 SIMD 版本。所以「generic 提供骨架、arch 作用域往同一命名空间补特化」是这套框架的惯用接线法——这也再次印证了 4.1 说的「定制点」思想。

## 5. 综合实践

把四个模块串起来，做一次「**为某个尺寸绘制完整的内存操作路线图**」的练习。

任务：假定默认 x86_64 配置（有 AVX2、`K_REP_MOVSB_THRESHOLD == SIZE_MAX`），分别针对 `memcpy(dst, src, 5)`、`memcpy(dst, src, 40)`、`memcpy(dst, src, 5000)` 三种调用，画出各自的执行路线。

要求完成：

1. **入口层**：三者都从 [memcpy.cpp:17-26](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memcpy.cpp#L17-L26) 进入，经 `inline_memcpy` 被 `flatten` 进符号体。
2. **编译期分派层**：三者都命中 [inline_memcpy.h:24-27](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/inline_memcpy.h#L24-L27) 的 x86 分支 → `inline_memcpy_x86_maybe_interpose_repmovsb` → 因阈值为 `SIZE_MAX` 转 `inline_memcpy_x86`。
3. **运行期分派层**（关键，需你填）：
   - `count=5`：命中 `count < 8`，走 `builtin::Memcpy<4>::head_tail`（写 `[0,4)` + `[1,5)`，区间 `[1,4)` 重叠写两次，值相同无害）。
   - `count=40`：AVX2 下 `VECTOR_SIZE=32`，命中 `count < 64` 段，走 `builtin::Memcpy<32>::head_tail`（写 `[0,32)` + `[8,40)`，区间 `[8,32)` 重叠写两次，值相同无害）。
   - `count=5000`：进入 [x86_64/inline_memcpy.h:81-91](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/x86_64/inline_memcpy.h#L81-L91) 的 `inline_memcpy_x86_avx_ge64`：先 `Memcpy<128>::head_tail` 处理前 256 字节并对齐 `dst`，再用 `builtin::Memcpy<64>::loop_and_tail` 反复搬 64 字节直到收尾。
4. **构建块层**：标注每条路线最终落在哪个作用域（本例全是 `builtin`），并指出 `block` 最终调到 [utils.h:84-108](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/utils.h#L84-L108) 的 `memcpy_inline` → `__builtin_memcpy_inline`。
5. **反思**：用一句话说明，为什么 `count=5` 和 `count=40` 都选 `head_tail` 而不是 `loop_and_tail`——因为它们都落在 `(SIZE, 2·SIZE]` 区间，两次定宽操作就能搞定，不必进入循环。

> 提示：若想核对 `count=40`、`count=5000` 的具体分支，务必对照 [x86_64/inline_memcpy.h:204-250](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/x86_64/inline_memcpy.h#L204-L250) 的真实条件，注意「`VECTOR_SIZE >= 32 ? count < 32 : count <= 32`」这种与向量宽度联动的写法。

## 6. 本讲小结

- mem\* 六函数（`memcpy`/`memmove`/`memset`/`bzero`/`bcmp`/`memcmp`）统一由 `block`/`tail`/`head_tail`/`loop_and_tail` 四个**构建块**组合而成；只有 `block` 必须真正实现，其余三者是建立在 `block` 之上的高阶函数。
- 把构建块的同一套接口交给不同实现，就得到三类**作用域**：`builtin`（交给 Clang 内建）、`generic`（纯 C++ 整数/向量）、架构特化（`rep;movsb`/SIMD intrinsic）；同一次调用可混用多个作用域。
- `memcpy` 入口点走**两级分派**：`inline_memcpy.h` 在编译期按开关/架构选实现（带 `[[gnu::flatten]]` 内联消除开销），具体实现（如 x86_64）在运行期按 `count` 选不同 `SIZE` 的构建块。
- `head_tail` 的「重叠写」是经典优化：用两次定宽操作覆盖 `(SIZE, 2·SIZE]` 整个区间，避免变宽处理与循环；重叠区被写两次，但对 `memset`（常量）与 `memcpy`（源/目不重叠）都是幂等的、无害的。
- 架构特化的 `rep;movsb` 是一个**可配置阈值**的可选项（`LIBC_COPT_MEMCPY_X86_USE_REPMOVSB_FROM_SIZE`），默认关闭，体现「是否受益依赖具体微架构」的审慎取舍。
- 整个 `memory_utils` 以 `add_header_library` 注册为内部头文件库（不产生公开 C 符号），由入口点经 CMake `DEPENDS` 引用，是 `__support` 设计哲学（`u4-l1`）的典型样本。

## 7. 下一步学习建议

- **对比 `memmove` 的不同**：阅读 [src/string/memory_utils/op_generic.h:219-350](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/string/memory_utils/op_generic.h#L219-L350) 的 `generic::Memmove`，重点看它为何要区分 `loop_and_tail_forward` / `loop_and_tail_backward` 以及 `align_forward`/`align_backward`——因为源和目标可能重叠，必须按方向决定搬运顺序。这是构建块思想在「更复杂语义」上的延伸。
- **看比较类函数如何复用框架**：阅读 `generic::Memcmp` / `generic::Bcmp`（同文件第 396–574 行），并结合 `op_x86.h` 的 SIMD 特化，理解「先判等、再算字典序」的两段式策略如何降低宽类型的比较成本。
- **进入数学库前先做一次微基准**：本讲是 `u9-l3`（基准测试）/ `u10-l3`（模糊与基准）的直接前置——建议在学完 `u6` 数学库后，回头用 `benchmarks/` 给 `memcpy` 的几个尺寸（1/16/64/4096）实测耗时，验证本讲讲的「按尺寸分派」带来的性能差异。
- **下一篇 `u6-l1`** 将离开字符串/内存领域，进入**数学库**的三层结构（入口点壳 → generic 算法 → `__support/math` 内联），你会看到本讲的「入口薄壳 + 算法下沉 + 多层特化」模式在数学函数上以另一种形式重演。
