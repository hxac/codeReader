# fat 胖指针操作与 Protected 类型守卫

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 Rust 中「瘦指针」与「胖指针（fat pointer）」的差别，理解指向 `dyn Trait` 的指针为什么是 **两个机器字** `(data, vtable)`，并能画出它的内存布局。
- 解释 `fat.rs` 里 `from_raw_parts` / `from_raw_parts_mut` / `vtable` 三个 `unsafe` 函数各自做什么、它们的**安全契约（safety contract）**是什么。
- 讲清为什么这三个函数都用 `mem::transmute_copy` 在 `FatPointer` 与胖指针之间转换，以及那行 `debug_assert_eq!(Layout::...)` 守卫的作用与代价。
- 解释 `Protected<T>` 作为一个 newtype「访问守卫」的设计意图：它不真正阻止访问，而是**强迫调用方在源码里写下一句 `'static` 理由字符串**。
- 独立阅读 `src/fat.rs` 与 `src/protected.rs`，并在一个最小示例里跑通「拆解胖指针 → 重组 → 调用方法」的 round-trip。

本讲是专家层第二篇，默认你已经读过 `u1-l2`（标准库类型的扩展 trait）。那里讲过的 `Static<T>` 用 newtype **改变了一个类型的判等/哈希语义**；本讲的 `Protected<T>` 思路相似——用 newtype **改变一个类型的「可访问性」**，把「能不能拿到内部值」从「随手 `.0`」提升为「必须写一句理由」。

## 2. 前置知识

- **trait object 与 `dyn Trait`**：Rust 用 `dyn Trait` 表示「实现了某 trait 的某个具体类型，但具体是哪个类型在编译期不固定」。这是 Rust 的动态分发（运行时多态）手段。例如 `&dyn Greet` 是「一个指向某 `Greet` 实现者的引用」。
- **胖指针（fat pointer）**：普通引用 `&T`（`T` 大小已知）是「瘦指针」，只占 1 个机器字（一个地址）。而指向**不定大小类型**（`dyn Trait` 或 `[T]`）的引用是「胖指针」，占 2 个机器字：一个地址 + 一段「元数据」。本讲只关心 `dyn Trait`，此时元数据是**指向虚表（vtable）的指针**。
- **虚表（vtable）**：编译器为每个「具体类型 + trait」组合生成的一张静态表，里面存着析构函数、大小、对齐，以及该 trait 各方法的函数指针。调用 `obj.method()` 时，运行时通过 vtable 找到真正的函数地址。
- **`unsafe` 与 `mem::transmute_copy`**：`transmute_copy` 把一段内存按「另一种类型」重新解读，是绕过类型系统的底层手段。本讲用它把 `(data, vtable)` 两个地址拼成的结构体「重铸」成一个胖指针。
- **`Layout` 与 `#[repr(C)]`**：`std::alloc::Layout` 描述一个类型的大小与对齐；`#[repr(C)]` 强制结构体字段按声明顺序、遵循 C ABI 布局，让我们能对内存布局做出可靠假设。
- **newtype 模式**：用 `struct Wrapper(T)` 包裹一个类型，借此改写它的 API。本讲 `Protected<T>` 就是典型 newtype。

## 3. 本讲源码地图

本讲围绕 `typst-utils` 里两个很小的文件展开：

| 文件 | 作用 |
| --- | --- |
| [src/fat.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/fat.rs) | 手动拆解/重组 `dyn Trait` 胖指针：`vtable`、`from_raw_parts`、`from_raw_parts_mut`，以及内部 `FatPointer` 表示 |
| [src/protected.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/protected.rs) | `Protected<T>` newtype：用 `access("理由")` 强制访问说明理由 |

它们在公共 API 中的导出位置：

| 文件 | 作用 |
| --- | --- |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/lib.rs) | `pub mod fat;`（整个模块直接公开）与 `pub use self::protected::Protected;`（只导出类型） |

为了说明「为什么要造这些轮子」，本讲还会引用两处真实调用方：

| 文件 | 作用 |
| --- | --- |
| crates/typst-macros/src/elem.rs | `fat::vtable` 的真实用法：为元素构建「能力 TypeId → vtable」映射表 |
| crates/typst-library/src/engine.rs | `Protected<Introspector>` 的真实用法：把「文档自省器」包起来，每次访问都要写理由 |

---

## 4. 核心概念与源码讲解

### 4.1 胖指针布局假设

#### 4.1.1 概念说明

先建立一个直觉。在 64 位机器上：

- 瘦指针 `&T`（`T: Sized`）：

```
+----------------+
| data address   |   1 个字 = 8 字节
+----------------+
```

- 胖指针 `&dyn Trait` 或 `&[T]`：

```
+----------------+----------------+
| data address   | vtable / len   |   2 个字 = 16 字节
+----------------+----------------+
```

对 `&dyn Trait` 而言，第二个字是**指向 vtable 的指针**。调用 `x.method()` 时，编译器生成「从 vtable 取方法地址 → 跳转」的代码。所以一个 `&dyn Trait` 在内存里就是「数据在哪 + 用哪张方法表」这两条信息。

**关键认识**：vtable 是编译期就固定好的静态数据。一旦确定了「源具体类型」和「目标 trait」，对应的 vtable 就嵌在二进制里、地址已知。这意味着——**只要类型信息齐全，我们就能凭空「拼」出一个胖指针**，而不必经过正常的 `as dyn Trait` 转换。这正是 `fat.rs` 想做的事：手动拆出 vtable，再手动拼回去。

#### 4.1.2 核心流程

`fat.rs` 用一个 `#[repr(C)]` 的结构体来**镜像**胖指针的内存表示：

```rust
#[repr(C)]
struct FatPointer {
    data: *const (),
    vtable: *const (),
}
```

`#[repr(C)]` 保证字段按声明顺序排列：先 `data`，后 `vtable`。这样 `FatPointer` 与真实的 `*const dyn Trait` 在内存里**逐位相同**（都是两个地址），从而可以互相转换。

整个模块的工作可以概括成一句话：**既然胖指针 == `(data, vtable)` 两个地址，那就用一个两字段结构体当搬运工，在「两个地址」与「真正的胖指针」之间反复横跳**。

#### 4.1.3 源码精读

模块开头的文档注释诚实地标注了这套机制的前提假设——见 [src/fat.rs:1-6](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/fat.rs#L1-L6)：

```rust
//! This assumes the memory representation of fat pointers. Although it is not
//! guaranteed by Rust, it's improbable that it will change. Still, when the
//! pointer metadata APIs are stable, we should definitely move to them:
//! <https://github.com/rust-lang/rust/issues/81513>
```

这段注释说明：Rust 语言规范**并未保证**胖指针就是 `(data, vtable)` 布局，但现实中所有主流编译器实现都如此，改动可能性极低；等到标准库的「指针元数据 API」（`Pointee`/`ptr::from_raw_parts` 等，对应 issue #81513）稳定后，应当迁移到官方 API，届时这段手写代码就可以删掉。

镜像结构体本身见 [src/fat.rs:56-64](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/fat.rs#L56-L64)：

```rust
#[repr(C)]
struct FatPointer {
    data: *const (),
    vtable: *const (),
}
```

它在 64 位下大小为 \( 2 \times 8 = 16 \) 字节，与 `*const dyn Trait` 完全一致；`#[repr(C)]` 锁定字段顺序，是后续 `transmute_copy` 能成立的地基。注意它是私有结构体，外部无法直接构造，只能通过三个公共函数间接使用。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `FatPointer` 与胖指针的「逐位相同」假设。

**操作步骤**：在你引入了 `typst-utils` 的临时项目里（任意一个 `cargo` 二进制 crate 即可），写下这段「示例代码」：

```rust
// 示例代码：仅观察布局，不调用 typst-utils
trait Greet { fn greet(&self); }
struct English;
impl Greet for English { fn greet(&self) { println!("hi"); } }

fn main() {
    let r: &dyn Greet = &English;
    println!("size of  &dyn Greet = {}", std::mem::size_of_val(&r));   // 胖指针大小
    println!("size of  &English   = {}", std::mem::size_of::<&English>()); // 瘦指针大小
    println!("2 * usize          = {}", 2 * std::mem::size_of::<usize>());
}
```

**需要观察的现象**：第一行打印「胖引用」自身的大小，第二行打印「瘦引用」大小。

**预期结果**：在 64 位机器上，胖引用为 16、瘦引用为 8，且 `2 * usize == 16`，正好对应 `FatPointer` 的两个字段。

> 若你在 32 位目标上运行，三个数会变成 8 / 4 / 8。本讲后续一律以 64 位为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `FatPointer` 的 `#[repr(C)]` 去掉，为什么 `transmute_copy` 转换可能出错？

**答案**：没有 `#[repr(C)]`，Rust 允许编译器重排字段顺序或加填充。若 `vtable` 被排到前面，`FatPointer` 的位模式就不再与真实胖指针一致，重铸出的指针会把 data 当成 vtable（或反之），调用方法时跳转到错误地址而未定义行为。

**练习 2**：为什么 `FatPointer` 用 `*const ()`（瘦指针）而不是 `*const dyn SomeTrait` 作为字段类型？

**答案**：字段类型要的是「一个普通地址」。`*const ()` 是瘦指针，占 1 个字，正好对应胖指针里的「data 字」或「vtable 字」。若字段本身又是胖指针，`FatPointer` 就会膨胀，不再镜像真实布局。

---

### 4.2 拆解与重组：vtable / from_raw_parts / from_raw_parts_mut

#### 4.2.1 概念说明

既然胖指针就是 `(data, vtable)`，自然能想到两个方向的操作：

- **拆解**：从一个现成的 `*const dyn Trait` 里，单独把 `vtable` 地址抠出来。
- **重组**：手里有 `data` 和 `vtable` 两个地址，把它们拼成一个全新的 `*const dyn Trait`。

为什么 Typst 需要这种能力？看一个真实场景。Typst 的元素（element）可以「实现某些能力 trait（capability）」，而运行时常常要回答：「给一个 `TypeId`，告诉我这个元素作为 `dyn 某能力` 的 vtable 在哪」。这个问题里，data 指针甚至可以是**悬空（dangling）**的——因为我们只关心 vtable，根本不会去读 data 指向的内存。这就是 `typst-macros` 里 `fat::vtable` 的用武之地（见 4.2.3 的真实用例）。

#### 4.2.2 核心流程

三个公共函数构成一对「拆」与「装」：

```
        拆解                                    重组
*const dyn Trait  ──vtable()──►  vtable 地址   ──┐
                      │                           ├──► from_raw_parts ──► *const dyn Trait
                      │                           │
*const dyn Trait  ──(取 data)──► data 地址  ────┘
                                                  ├──► from_raw_parts_mut ──► *mut dyn Trait
                                                  │     （同上，但产出可变指针）
```

注意 `vtable()` 的返回类型是 `NonNull<()>`——一个非空瘦指针，方便调用方再拿去重组。

每个函数都带着**安全契约**：调用方必须保证 `T` 确实是 `dyn Trait`、`data` 指向的值确实实现了该 trait、且 `vtable` 必须来自本模块的 `vtable()` 函数。这些约定写在各自的 `# Safety` 文档注释里，编译器无法检查，全靠人。

#### 4.2.3 源码精读

先看**拆解**函数 `vtable`——[src/fat.rs:42-54](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/fat.rs#L42-L54)：

```rust
/// Extract the address to a trait object's vtable.
///
/// # Safety
/// Must only be called when `T` is a `dyn Trait`.
#[track_caller]
pub unsafe fn vtable<T: ?Sized>(ptr: *const T) -> NonNull<()> {
    unsafe {
        debug_assert_eq!(Layout::new::<*const T>(), Layout::new::<FatPointer>());
        NonNull::new_unchecked(
            mem::transmute_copy::<*const T, FatPointer>(&ptr).vtable as *mut (),
        )
    }
}
```

这段代码把传入的胖指针 `*const T` 重铸成 `FatPointer`，直接取它的 `.vtable` 字段，再用 `NonNull::new_unchecked` 包成非空指针。**它只读取 vtable 字，从不解引用 `data`**——这一点对后面的「悬空指针」用例至关重要。

再看两个**重组**函数，它们互为镜像——先看 `from_raw_parts`，见 [src/fat.rs:12-25](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/fat.rs#L12-L25)：

```rust
/// Create a fat pointer from a data address and a vtable address.
///
/// # Safety
/// Must only be called when `T` is a `dyn Trait`. The data address must point
/// to a value whose type implements the trait of `T` and the `vtable` must have
/// been extracted with [`vtable`].
#[track_caller]
pub unsafe fn from_raw_parts<T: ?Sized>(data: *const (), vtable: *const ()) -> *const T {
    unsafe {
        let fat = FatPointer { data, vtable };
        debug_assert_eq!(Layout::new::<*const T>(), Layout::new::<FatPointer>());
        mem::transmute_copy::<FatPointer, *const T>(&fat)
    }
}
```

它把 `(data, vtable)` 装进 `FatPointer`，再重铸成 `*const T`。`from_raw_parts_mut` 完全同构，只是产出 `*mut T`，见 [src/fat.rs:27-40](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/fat.rs#L27-L40)：

```rust
#[track_caller]
pub unsafe fn from_raw_parts_mut<T: ?Sized>(data: *mut (), vtable: *const ()) -> *mut T {
    unsafe {
        let fat = FatPointer { data, vtable };
        debug_assert_eq!(Layout::new::<*mut T>(), Layout::new::<FatPointer>());
        mem::transmute_copy::<FatPointer, *mut T>(&fat)
    }
}
```

三个函数都标注了 `#[track_caller]`：一旦里面的 `debug_assert` 失败触发 panic，错误栈会指向**调用方**而不是 `fat.rs` 内部，便于排查是哪处 `unsafe` 用错了。

**真实用例**：`typst-macros` 在为元素生成「能力 vtable 查找表」时用了 `fat::vtable`，见 [crates/typst-macros/src/elem.rs:683-693](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/elem.rs#L683-L693)：

```rust
let checks = relevant.map(|capability| {
    quote! {
        if capability == ::std::any::TypeId::of::<dyn #capability>() {
            // Safety: The vtable function doesn't require initialized
            // data, so it's fine to use a dangling pointer.
            return Some(unsafe {
                ::typst_utils::fat::vtable(dangling as *const dyn #capability)
            });
        }
    }
});
```

这段由宏展开后，会生成一个闭包：传入一个 `TypeId`，遍历该元素所有「能力」，命中就返回对应 `dyn 能力` 的 vtable。`dangling` 是一个**悬空但非空、且对齐合法**的指针（来自 `NonNull::dangling()`）。注释点明了为什么用悬空指针是安全的：`vtable()` 只读 vtable 字、不碰 data，所以「数据未初始化」也无妨。

#### 4.2.4 代码实践

**实践目标**：跑通「拆解 → 重组 → 调用」的完整 round-trip，亲手验证拼回的胖指针能正常分发方法。

**操作步骤**：在临时二进制 crate 的 `Cargo.toml` 里加上 `typst-utils = { path = "..." }`（或从 crates.io / git 引入），写入这段「示例代码」：

```rust
// 示例代码
use typst_utils::fat;

trait Greet {
    fn greet(&self);
}

struct English;
impl Greet for English {
    fn greet(&self) {
        println!("Hello from English!");
    }
}

fn main() {
    let value = English;
    // 一个真实的 &dyn Greet 胖指针
    let ref_dyn: &dyn Greet = &value;

    // ① 拆解：data 用具体值的地址，vtable 用 fat::vtable 取出
    let data = &value as *const English as *const ();
    let vtable = unsafe { fat::vtable(ref_dyn) };

    // ② 重组：把 (data, vtable) 拼回 *const dyn Greet
    let rebuilt: *const dyn Greet =
        unsafe { fat::from_raw_parts(data, vtable.as_ptr()) };

    // ③ 通过重组后的胖指针调用方法，验证 round-trip 成功
    unsafe {
        (&*rebuilt).greet();
    }
}
```

**需要观察的现象**：第 ③ 步是否正确打印出问候语。

**预期结果**：打印 `Hello from English!`，说明用 `from_raw_parts` 手工拼出的胖指针与原 `&dyn Greet` 行为一致。

> 说明：`data` 必须指向「真正实现了 `Greet` 的值」（这里是 `English`），`vtable` 必须来自 `fat::vtable`——两者都满足，因此满足 `from_raw_parts` 的安全契约。若你故意把 `data` 换成别的类型，运行结果将为未定义行为。

#### 4.2.5 小练习与答案

**练习 1**：把上面示例里 `data` 改成 `std::ptr::null()`，但 `vtable` 仍取自 `ref_dyn`，然后**只调用 `fat::vtable`**（不调用 `greet`）。会出问题吗？

**答案**：不会。`vtable()` 只读 vtable 字、不解引用 data，所以 data 即使是 null 也无所谓。这正是 `typst-macros` 敢用 `dangling`/悬空指针的依据。但若再对 `from_raw_parts` 的结果解引用（如调用 `greet`），就会解引用 null 而崩溃。

**练习 2**：`vtable()` 的返回类型为什么是 `NonNull<()>` 而不是 `*const ()`？

**答案**：vtable 永远非空（合法 trait object 必有 vtable），用 `NonNull` 在类型层面表达这一不变量，并在 `from_raw_parts` 重组时方便调用方传回 `.as_ptr()`。这也契合 Rust 对「非空原始指针」推荐的 `NonNull` 写法。

---

### 4.3 transmute_copy 转换与 Layout 守卫

#### 4.3.1 概念说明

4.2 里三个函数都靠同一招实现：`mem::transmute_copy`。这是本节要单独讲清的「底层机关」。

`mem::transmute_copy::<Src, Dst>(&src)` 的语义是：从 `&src` 起读 `size_of::<Src>()` 字节，然后**把这串字节当成 `Dst` 类型返回**。它和 `mem::transmute` 类似，都是「按位重铸」，但更灵活：

- `transmute` 要求 `Src` 与 `Dst` **编译期大小完全相等**，且对泛型类型检查极严。本讲里 `T: ?Sized`，`*const T` 是 DST 指针，`transmute` 对此并不友好。
- `transmute_copy` 则按「源的实际字节数」读取，绕开了静态大小校验，代价是**正确性责任完全交给人**。

正因如此，`fat.rs` 选择 `transmute_copy`，并配上一道 `debug_assert` 当「安全网」。

#### 4.3.2 核心流程

三个函数的转换流程完全一致，以 `from_raw_parts` 为例：

```
          FatPointer { data, vtable }            （16 字节，两个字段地址）
                    │
                    │  mem::transmute_copy::<FatPointer, *const T>(&fat)
                    │  ——按位重铸——
                    ▼
              *const T  （*const dyn Trait，16 字节胖指针）
```

转换成立的前提只有一条：**`FatPointer` 与 `*const T`（`T` 为 `dyn Trait`）大小、对齐都相同**。这条前提由那行 `debug_assert_eq!` 在调试构建里持续校验。

#### 4.3.3 源码精读

守卫语句在三个函数里都一样（以 `from_raw_parts` 为例），见 [src/fat.rs:22](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/fat.rs#L22)：

```rust
debug_assert_eq!(Layout::new::<*const T>(), Layout::new::<FatPointer>());
```

`Layout::new::<X>()` 返回 `X` 的大小与对齐组成的 `Layout`。两个 `Layout` 相等，意味着二者**大小相同、对齐相同**。`debug_assert_eq!` 只在 `debug` profile 生效，`release` 构建里被完全移除、零运行时开销。它的作用是：万一未来某平台真的改变了胖指针布局（违背 4.1.1 的假设），调试构建会立刻 panic，把 bug 暴露在开发阶段，而不是悄悄产生未定义行为。

紧接着的重铸——[src/fat.rs:23](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/fat.rs#L23)：

```rust
mem::transmute_copy::<FatPointer, *const T>(&fat)
```

把 16 字节的 `FatPointer` 原样重铸成 `*const T`。注意 `&fat`：`transmute_copy` 取的是引用，从该地址读 `size_of::<FatPointer>()`（= 16）字节。目标 `*const T` 同样是 16 字节，二者匹配，重铸安全。

> 为什么「拆解」方向 `vtable()` 里写的是 `transmute_copy::<*const T, FatPointer>(&ptr)`？因为这里源是胖指针、目标是 `FatPointer`，方向反过来而已，原理相同。

#### 4.3.4 代码实践

**实践目标**：体会 `debug_assert` 的「开发期守卫、发布期零开销」语义。

**操作步骤**：

1. 在 4.2.4 的示例基础上，把 `from_raw_parts` 调用包进一个自定义函数，并在函数里手动加一行与源码等价的断言：

   ```rust
   // 示例代码
   fn dbg_check<T: ?Sized>() {
       debug_assert_eq!(
           std::alloc::Layout::new::<*const T>(),
           std::alloc::Layout::new::<(usize, usize)>(),
       );
   }
   ```

2. 分别用 `cargo run`（默认 debug）与 `cargo run --release` 运行。

**需要观察的现象**：debug 构建里断言是否被求值；release 构建里是否还能看到断言相关代码。

**预期结果**：debug 构建正常通过（因为胖指针确实是两个 `usize`）；release 构建里断言被完全移除，对性能无影响。

> 若想直观看到断言「生效」，可临时把断言改成 `assert_eq!(Layout::new::<*const T>(), Layout::new::<u8>())`（故意不等），debug 运行会立刻 panic。

#### 4.3.5 小练习与答案

**练习 1**：为什么这里用 `debug_assert_eq!` 而不是 `assert_eq!`？

**答案**：胖指针布局在所有主流平台上都是稳定的，这是一道「理论上可能、实际不会」的防线。用 `debug_assert` 让它在开发期把关、在生产期零开销；若用 `assert`，每次调用都要比较两个 `Layout`，违背了这个底层工具「极低成本」的初衷。

**练习 2**：`transmute_copy` 比 `transmute` 多读了一个 `&`（`&fat`）。这个引用在转换中起什么作用？

**答案**：`transmute_copy` 通过引用定位源数据，再按 `size_of::<Src>()` 字节读取后重铸。这种方式不要求编译器在静态阶段证明 `Src` 与 `Dst` 大小相等，从而能处理 `T: ?Sized` 这类 DST 指针；代价是大小匹配的正确性由开发者用断言自行担保。

---

### 4.4 Protected<T> 访问守卫

#### 4.4.1 概念说明

换一个完全不同的主题。`fat.rs` 关心的是「指针的位」，`protected.rs` 关心的是「类型的 API 纪律」。

问题背景：Typst 编译器在排版时会反复做「自省（introspection）」——查询「这个标签在第几页」「这个计数器当前值是多少」之类。这类信息来自一个叫 `Introspector` 的对象。**但 Introspector 的值会随排版迭代而变化**，只有在你「确实处于自省阶段」时读取它才是合法的；别的时候随手读，可能读到陈旧、不一致的数据。

Rust 没法在类型系统里表达「现在是不是自省阶段」。那怎么减少误用？`Protected<T>` 的答案很朴素：**把值包进一个 newtype，让「取出内部值」这件事必须显式写一句理由**。它不真正阻止你访问——它只是让你在源码里留下 `access("为什么这次访问是 OK 的")` 这么一行，把「这次访问」变成一个**可被 code review、可被 grep 出来的审计点**。

这与 `u1-l2` 的 `Static<T>` 思路同源：都是用 newtype 改写一个类型的「便利性」。`Static` 让判等退化为指针比较；`Protected` 让「拿内部引用」从「写个 `.0`」升级为「写一句理由」。

#### 4.4.2 核心流程

`Protected<T>` 的全部 API 只有四个方法，分两类：

```
            ┌──────────────── 包装 ────────────────┐
            │                                      │
            ▼                                      │
   T ──new()──► Protected<T>                   from_raw()
            │                                      ▲
            │ access("理由")                       │
            ▼                                      │
           &T  （只读借用，需写理由）            Protected<T>
            │                                      ▲
            │ into_raw()                           │
            ▼                                      │
           T  ────────────────────────────────────┘
             （取出内部值，无需理由；约定只能用于 from_raw 重新包装）
```

- **`access(&self, _justification: &'static str) -> &T`**：正常访问通道。注意 `_justification` 前有下划线——**这个参数在运行时根本不被使用**，它纯粹是「写在源码里给人看」的。
- **`into_raw(self) -> T` + `from_raw(inner) -> Self`**：「逃逸通道」。当你已经在上游正当访问过这个值、现在只是要把它在各处搬运并重新包装时，用 `into_raw` 取出、用 `from_raw` 装回，免去反复写理由。

#### 4.4.3 源码精读

结构体定义见 [src/protected.rs:1-6](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/protected.rs#L1-L6)：

```rust
/// Wraps a type, requiring justification on access.
///
/// On the type-system level, this does not do much, but it makes sure that
/// users of the value think twice and justify their use.
#[derive(Debug, Copy, Clone)]
pub struct Protected<T>(T);
```

文档非常坦诚：「在类型系统层面它没做什么，但能确保使用者三思而后行、并写下理由」。`derive(Copy, Clone)` 让 `T: Copy` 时整个包装也可拷贝，便于在结构体字段里廉价传递。

注意一个关键设计：**`Protected` 没有实现 `Deref`/`DerefMut`**。如果它实现了 `Deref`，`secret.method()` 就会自动解引用、绕过 `access()`，整个守卫就形同虚设。这正是它与「普通智能指针」的根本区别。

四个方法见 [src/protected.rs:8-32](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-utils/src/protected.rs#L8-L32)：

```rust
impl<T> Protected<T> {
    /// Wrap a value of type `T`.
    pub fn new(inner: T) -> Self {
        Self(inner)
    }

    /// Rewrap a value extracted via [`into_raw`](Self::into_raw).
    ///
    /// This is distinct from [`new`](Self::new) as it's only meant to be used
    /// for rewrapping and not for initial wrapping.
    pub fn from_raw(inner: T) -> Self {
        Self(inner)
    }

    /// Extract the inner value without justification. The result may only be
    /// used with [`from_raw`](Self::from_raw).
    pub fn into_raw(self) -> T {
        self.0
    }

    /// Access the underlying value, providing justification why it's okay.
    pub fn access(&self, _justification: &'static str) -> &T {
        &self.0
    }
}
```

要点逐条解读：

- `new` 与 `from_raw` 函数体一模一样（都是 `Self(inner)`），但**语义不同**：`new` 用于「首次包装」，`from_raw` 专用于「重新包装从 `into_raw` 取出的值」。分成两个名字，是为了在 grep / review 时一眼区分「这里是新创建一个受保护值」还是「这里只是搬运一个已经放行过的值」。
- `into_raw` 的文档明确警告「结果只能配合 `from_raw` 使用」——即取出后应重新包装，而不是直接拿来用，否则就绕过了守卫。
- `access` 的 `_justification: &'static str`：`'static` 限定意味着理由必须是**编译期字符串字面量**，不能塞入运行期动态拼接的 `String`。这保证每一条理由都能在源码里直接定位，构成可审计的证据链。

**真实用例**：`typst-library` 把 Introspector 包成 `Protected`，见 [crates/typst-library/src/engine.rs:27-28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L27-L28)：

```rust
/// Provides access to information about the document.
pub introspector: Protected<Tracked<'a, dyn Introspector + 'a>>,
```

当确实要在「记录自省结果」这一正当场景访问它时，调用方写下了理由，见 [crates/typst-library/src/engine.rs:113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L113)：

```rust
let introspector = *self.introspector.access("is okay since we're recording it");
```

理由字符串 `"is okay since we're recording it"` 直接嵌在源码里，任何 code review 都能立刻看到「为什么这里可以读 introspector」。整个 Typst 代码库里类似 `let introspector = Protected::from_raw(introspector);` 的「拆包→搬运→重新包装」模式出现十几次（见 typst-layout、typst-eval、typst-html 等），都是 `into_raw`/`from_raw` 逃逸通道的实战。

#### 4.4.4 代码实践

**实践目标**：亲手体会 `access`（需理由）与 `into_raw`/`from_raw`（逃逸通道）两类 API 的语义差别。

**操作步骤**：在临时二进制 crate 里写入这段「示例代码」：

```rust
// 示例代码
use typst_utils::Protected;

fn main() {
    // 用 Protected 包裹一个「敏感」值（String 非 Copy，便于观察所有权转移）
    let secret = Protected::new(String::from("top-secret"));

    // ① 正常访问：必须传入一个 'static 理由字符串字面量
    let s: &String = secret.access("审计需要读取口令");
    println!("access  -> {s}");

    // ② 逃逸通道：into_raw 取出内部值（消费 secret），from_raw 重新包装
    let raw: String = secret.into_raw();          // 取出，无需理由
    let rewrapped = Protected::from_raw(raw);     // 重新装回，约定只用于 rewrap

    println!("rewrapped -> {}", rewrapped.access("二次审计"));
}
```

**需要观察的现象**：

1. 第 ① 步能否在「不写字面量理由」的情况下编译通过？
2. `into_raw` 之后，原 `secret` 是否还能继续使用（提示：`String` 不是 `Copy`）？
3. `rewrapped` 与原值内容是否一致？

**预期结果**：

1. 删掉理由字符串会编译失败（参数个数不匹配）——这正是「强制写理由」的体现。
2. `into_raw(self)` 取得了所有权，`secret` 已被移动，之后再用会报「use of moved value」。
3. 重新包装后的内容仍是 `top-secret`。

> 说明：由于 `Protected` 派生了 `Copy`/`Clone`，当 `T: Copy`（如 `Protected<i32>`）时 `into_raw` 实际是复制而非移动，原绑定仍可用；这里特意用 `String` 让所有权转移更显眼。

#### 4.4.5 小练习与答案

**练习 1**：`access` 的理由参数在运行时被使用了吗？既然没被使用，为什么还要它？

**答案**：没有，参数名带下划线 `_justification` 表示未使用。保留它纯粹是为了「把理由写进源码」——这是一种社会工程式的守卫，靠 code review 与可搜索性来约束，而非靠运行时检查。把它限定为 `'static str` 还确保理由必须是字面量，无法用动态字符串糊弄。

**练习 2**：`new` 和 `from_raw` 函数体完全相同，为什么不合并成一个？

**答案**：为了语义可读性与可审计性。「首次包装」与「重新包装放行过的值」是两种不同的意图。分成两个名字后，review 时一眼能区分；`into_raw` 的文档也明确要求其结果「只能配合 `from_raw`」。这和 `Protected` 整体哲学一致：用 API 形状引导正确用法。

**练习 3**：假如给 `Protected` 加上 `impl Deref for Protected`，会带来什么后果？

**答案**：会致命地破坏守卫。有了 `Deref`，`secret.some_method()` 会自动解引用取到 `&T`，调用方无需写理由就能用，整个 `access` 机制被绕过。所以 `Protected` 刻意**不**实现 `Deref`/`DerefMut`。

---

## 5. 综合实践

把本讲两个主题串起来，完成下面这个「最小可运行」的小任务。

**任务背景**：假设你有一个「敏感的动态分发对象」，既要用 `fat` 机制手动管理它的胖指针，又要把对它的访问纳入 `Protected` 守卫。

**要求**：

1. 定义一个 trait（如 `Diagnostic { fn describe(&self) -> String; }`）和一个实现它的具体类型。
2. 用 `fat::vtable` + `fat::from_raw_parts` 完成一次「拆解 → 重组」的 round-trip，并通过重组指针调用方法。
3. 把「重组得到的 `&dyn Diagnostic`」所引用的具体值，用 `Protected::new` 包裹起来；之后**分别**用：
   - `access("需要打印诊断")` 取得引用并打印 `describe()`；
   - `into_raw` + `from_raw` 做一次「拆包→重新包装」，再次 `access` 打印。

**参考实现骨架（示例代码）**：

```rust
// 示例代码
use typst_utils::{fat, Protected};

trait Diagnostic {
    fn describe(&self) -> String;
}

struct Warning {
    code: u32,
}

impl Diagnostic for Warning {
    fn describe(&self) -> String {
        format!("warning #{}", self.code)
    }
}

fn main() {
    let value = Warning { code: 42 };
    let ref_dyn: &dyn Diagnostic = &value;

    // ① fat round-trip：拆解再重组
    let data = &value as *const Warning as *const ();
    let vt = unsafe { fat::vtable(ref_dyn) };
    let rebuilt: *const dyn Diagnostic = unsafe { fat::from_raw_parts(data, vt.as_ptr()) };
    let via_fat: &dyn Diagnostic = unsafe { &*rebuilt };
    println!("via fat  -> {}", via_fat.describe());

    // ② Protected 守卫：把同一个值包起来
    let protected = Protected::new(Warning { code: 42 });
    println!("access   -> {}", protected.access("需要打印诊断").describe());

    // ③ 逃逸通道：拆包再重新包装
    let raw = protected.into_raw();
    let rewrapped = Protected::from_raw(raw);
    println!("rewrapped-> {}", rewrapped.access("二次打印").describe());
}
```

**预期结果**：三行打印都输出 `warning #42`（前缀分别为 `via fat`、`access`、`rewrapped`）。

**延伸思考**（可选，无需写代码）：如果 `via_fat` 与 `protected` 指向的是**两个不同的 `Warning` 值**（如上例，一个来自栈上的 `value`，一个是 `Protected::new` 新建的），它们各自的生命周期如何保证？把 `protected` 改成包「`via_fat` 所引用的同一个 `value`」会遇到什么所有权问题？这能帮你理解 `fat`（操作裸指针、需手动管生命周期）与 `Protected`（操作值、靠所有权保证安全）两者适用场景的边界。

## 6. 本讲小结

- **胖指针 = `(data, vtable)`**：指向 `dyn Trait` 的引用/指针占两个机器字，第二个字是 vtable 地址；`fat.rs` 用 `#[repr(C)]` 的 `FatPointer` 结构体镜像这一布局。
- **三个函数 = 拆与装**：`vtable()` 抠出 vtable 地址（且不解引用 data，故悬空 data 也安全）；`from_raw_parts` / `from_raw_parts_mut` 用 `(data, vtable)` 拼回胖指针，互为镜像。每个都带「`T` 必须是 `dyn Trait`」等安全契约。
- **`transmute_copy` + `debug_assert`**：转换靠 `mem::transmute_copy` 按位重铸；`debug_assert_eq!(Layout::...)` 在调试构建里校验两边大小/对齐一致，发布构建里零开销。
- **真实驱动场景**：`typst-macros` 用 `fat::vtable` 为元素构建 `TypeId → vtable` 映射，并利用「vtable 不读 data」安全地用悬空指针取表。
- **`Protected<T>` 是「写理由」守卫**：newtype 不实现 `Deref`，逼迫调用方写 `access("理由")`；理由限定为 `'static str`，构成可 grep、可审计的源码证据链。`into_raw`/`from_raw` 是「已放行值的搬运」逃逸通道。
- **二者共同点**：都用「改写 API 形状」来表达纪律——`fat` 改的是指针的位操作，`Protected` 改的是值的可访问性，且都诚实标注了自身能力边界（`fat` 标注布局假设、`Protected` 标注「类型系统层面作用有限」）。

## 7. 下一步学习建议

- **接续专家层**：下一篇 `u3-l3` 讲 `Deferred` 后台并行与 `defer()` RAII。`defer()` 同样是「用类型生命周期表达纪律」（作用域结束自动还原状态），可与本讲 `Protected` 的「newtype 守卫」思路对照阅读。
- **深入胖指针与指针元数据**：阅读 [rust-issue #81513](https://github.com/rust-lang/rust/issues/81513)（pointer metadata API 跟踪 issue），理解未来 `fat.rs` 可能迁移到的官方 `ptr::from_raw_parts` 等接口；尝试用 nightly 的 `ptr::from_raw_parts` 重写本讲的 round-trip 示例，对比两种写法。
- **顺藤摸瓜读真实用例**：通读 [crates/typst-macros/src/elem.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/elem.rs) 里 `fat::vtable` 周边（生成「能力 vtable 查找表」的宏），理解 Typst 元素如何用 vtable 实现 `dyn Capability` 的动态分发；再 grep `Protected::from_raw` 统计 `into_raw`/`from_raw` 逃逸通道在 typst-layout / typst-eval 中的使用密度，体会这套守卫在大型代码库里的实际成本与收益。
