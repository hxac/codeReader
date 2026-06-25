# 安全描述符与权限控制

## 1. 本讲目标

本讲聚焦 interprocess 在 Windows 上提供的「安全描述符（Security Descriptor, SD）」抽象，回答三个问题：

1. Windows 用什么数据结构描述「谁能访问一个对象」？named pipe 在其中扮演什么角色？
2. interprocess 怎样把这个 C 风格的、容易写错的 Win32 结构，封装成带所有权的 Rust 类型？
3. 如何把一个自定义安全描述符挂到 local socket 监听器上，从而精确控制「哪些进程能连进来」？

学完本讲，你应当能够：

- 说清 **DACL / SACL / SID / ACE** 四个概念，以及最危险的「**null DACL**」与「**absent DACL**」的区别。
- 区分 `SecurityDescriptor`（拥有）、`BorrowedSecurityDescriptor`（共享借用）、`MutBorrowedSecurityDescriptor`（可变借用）三种表示，以及它们各自的安全不变式。
- 跟踪一条从 `ListenerOptions::security_descriptor` 一路下沉到 Win32 `CreateNamedPipeW` 的完整调用链。
- 写出一个带自定义安全描述符（或 null DACL）的 named pipe 监听器，并解释它对客户端访问的影响。

## 2. 前置知识

本讲默认你已经学过 **u4-l3（Windows 原生 named pipe API）**，知道 named pipe 是 Windows 的一种「可命名、可多实例、可双工」的 IPC 原语，也知道 `CreateNamedPipeW` 是创建服务端管道实例的入口。下面补充几个 Windows 安全模型的基础概念，它们是理解源码的前提。

- **可安全对象（securable object）**：Windows 中几乎所有能被「打开/访问」的内核对象——文件、进程、named pipe、事件……——都是可安全对象。每个可安全对象都关联一个**安全描述符**，描述「谁能对它做什么」。
- **SID（Security Identifier）**：用来唯一标识一个用户、组或登录会话的变长二进制串，形如 `S-1-5-32-544`（内置 Administrators 组）。
- **ACE（Access Control Entry，访问控制项）**：一条规则，形如「允许/拒绝 某个 SID 的某些访问权限」。
- **ACL（Access Control List，访问控制列表）**：一串 ACE 的有序列表。分为两类：
  - **DACL（Discretionary ACL）**：由对象所有者设定、决定「谁能访问」的列表，本讲的主角。
  - **SACL（System ACL）**：决定「哪些访问要被审计记录」，与鉴权无关。
- **SECURITY_ATTRIBUTES**：Win32 创建对象时传入的小结构，其中 `lpSecurityDescriptor` 字段就指向上面那个安全描述符。这正是 named pipe 与 SD 的接口点。

> 一个直觉比喻：安全描述符像一份「门禁名单」。owner/group 是房间主人，DACL 是「白名单/黑名单条目」，SACL 是「摄像头记录规则」。named pipe 服务端在创建管道实例时把这份名单交给内核，之后每一次 `CreateFile` 连接都要先过名单这一关。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/os/windows/security_descriptor.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor.rs) | 模块根：声明子模块、统一再导出、提供 `validate()` 与 `create_security_attributes()`。 |
| [src/os/windows/security_descriptor/owned.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/owned.rs) | 拥有型 `SecurityDescriptor`：按值持有 SD 并拥有其全部 ACL/SID。 |
| [src/os/windows/security_descriptor/borrowed.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/borrowed.rs) | 借用型 `BorrowedSecurityDescriptor` 与 `MutBorrowedSecurityDescriptor`。 |
| [src/os/windows/security_descriptor/as_security_descriptor.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/as_security_descriptor.rs) | `unsafe trait AsSecurityDescriptor`：统一的「取裸指针」抽象及其安全契约。 |
| [src/os/windows/security_descriptor/ext.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/ext.rs) | 扩展 trait：读写 DACL/SACL/owner/group、序列化、写入 `SECURITY_ATTRIBUTES`。 |
| [src/os/windows/security_descriptor/try_clone.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/try_clone.rs) | 深拷贝（`clone`）与本地堆包装 `LocalBox`。 |
| [src/os/windows/security_descriptor/c_wrappers.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/c_wrappers.rs) | 对裸 Win32 调用的 `unsafe` 封装与错误转换。 |
| [src/local_socket/listener/options.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs) | 公共 `ListenerOptions`：含 `security_descriptor` 字段。 |
| [src/os/windows/local_socket.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket.rs) | Windows 专有扩展 trait `ListenerOptionsExt`，提供 `.security_descriptor()` setter。 |
| [src/os/windows/named_pipe/local_socket/listener.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs) | Windows 后端：把公共 `ListenerOptions` 翻译成 `PipeListenerOptions`。 |
| [src/os/windows/named_pipe/listener/create_instance.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs) | 真正调用 `CreateNamedPipeW` 的地方，SD 在此注入。 |

## 4. 核心概念与源码讲解

### 4.1 安全描述符的四要素与 null DACL 陷阱

#### 4.1.1 概念说明

一个安全描述符由四类信息外加一组控制位组成：

| 组成 | 含义 | interprocess 中的访问方法 |
| --- | --- | --- |
| **Owner SID** | 对象所有者 | `owner()` / `set_owner()` |
| **Group SID** | 对象主组 | `group()` / `set_group()` |
| **DACL** | 决定谁能访问的 ACL（本讲主角） | `dacl()` / `set_dacl()` / `unset_dacl()` |
| **SACL** | 审计规则 ACL | `sacl()` / `set_sacl()` / `unset_sacl()` |

这里有一个**极易踩坑、且源码反复强调**的区别：

- **absent DACL（未设置）**：描述符里「没有 DACL」这一项。新建对象的默认安全描述符就是这种状态，Windows 会回退到从父对象或进程令牌派生的默认权限。
- **null DACL（空 DACL）**：描述符里「有 DACL，但 DACL 是个空指针」。这不是「没人能访问」，恰恰相反——**它意味着任何安全主体都拥有全部访问权限**。

这个区别在 ext.rs 中被明确写进文档，值得逐字阅读：

[src/os/windows/security_descriptor/ext.rs:48-66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/ext.rs#L48-L66) — `set_acl` 间接方法的文档注释，其中第 56–57 行明确警告：「a null ACL (`ptr::null_mut()`) is not the same as an unset/absent ACL: it actually provides **full access** for every security principal」。

> 名字相近、语义相反，是 Windows 安全 API 最经典的「语义陷阱」。interprocess 选择用 `unset_dacl()`（设置 present=false）与 `set_dacl(ptr::null_mut(), …)`（设置 present=true、指针为空）两个不同的方法来把这两种状态区分开，而不是合并成一个开关。

#### 4.1.2 核心流程

当一个客户端尝试 `CreateFile` 连接到一个 named pipe 时，Windows 的安全引用监视器（SRM）会：

1. 取出该管道实例的安全描述符；
2. 检查其 DACL 状态：
   - **absent DACL** → 使用默认访问检查（通常允许同令牌的访问）；
   - **null DACL** → 直接授予**全部**请求权限，等同于「不设防」；
   - **正常 DACL** → 按 ACE 顺序逐条匹配客户端 SID，命中「拒绝」则拒，命中「允许」则放，全不命中则拒。
3. 据此放行或拒绝连接请求。

因此，服务端只需在 `CreateNamedPipeW` 时传入正确的 SD，就能在内核层把「不被信任的客户端」挡在门外，而无需自己在应用层做鉴权。

#### 4.1.3 源码精读

模块根的文档点明了设计取向：interprocess **不**替你构造复杂的 SD，而是提供「可组合的原语」，让你自行构造或借助别的 crate：

[src/os/windows/security_descriptor.rs:1-8](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor.rs#L1-L8) — 模块文档：声明「构造 SD 很复杂、超出 interprocess 范围」，本模块只提供强调可组合性的原语。

`set_acl` 在 c_wrappers 层的关键三行，精确刻画了「present 位 + 指针」如何编码上述三种状态：

[src/os/windows/security_descriptor/c_wrappers.rs:55-66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/c_wrappers.rs#L55-L66) — `has_acl = acl.is_some()` 决定 present 位；当内部调用方传 `None`（即 `unset_*`）时 `has_acl=false`、指针为 null，注释说明此时 null 指针被 Windows 忽略；而 ext 层的 `set_*` 永远传 `Some(ptr)`，于是 `set_dacl(ptr::null_mut(), …)` 会得到 present=true、指针=null 的 null DACL。

#### 4.1.4 代码实践

**实践目标**：用阅读源码的方式，把 null DACL 与 absent DACL 的区别内化。

**操作步骤**：

1. 打开 `src/os/windows/security_descriptor/ext.rs`，找到 `indirect_methods!` 宏里 `set_acl` 与 `unset_acl` 两个分支的文档。
2. 对照 `src/os/windows/security_descriptor/c_wrappers.rs` 的 `set_acl` 与 `unset_acl`，确认 `has_acl` 取值。
3. 在纸上画一张表，列出三种调用 `SetSecurityDescriptorDacl(sd, bDaclPresent, pDacl, bDaclDefaulted)` 的语义：
   - `(FALSE, NULL, _)` → absent DACL；
   - `(TRUE, NULL, _)` → null DACL = **全开**；
   - `(TRUE, &acl, _)` → 受 `acl` 控制。

**需要观察的现象 / 预期结果**：你能不看文档复述「present 位」与「指针是否为空」这两个维度各自控制什么。

#### 4.1.5 小练习与答案

**练习 1**：如果你想让 named pipe「只允许本机 Administrators 组连接」，应该用哪种 DACL？

> **参考答案**：构造一个正常 DACL，包含「允许 Administrators（`S-1-5-32-544`）所需访问权限」的 ACE，再用 `set_dacl(&acl, false)` 设置（present=true，指针非空）。绝不能用 null DACL——那是全开。

**练习 2**：`unset_dacl()` 与 `set_dacl(ptr::null_mut(), false)` 在内核访问检查时结果是否相同？

> **参考答案**：不同。`unset_dacl()` 让 DACL「缺席」，走默认安全；`set_dacl(ptr::null_mut(), false)` 是 null DACL，授予所有人全部权限。

### 4.2 SecurityDescriptor 的三种所有权表示

#### 4.2.1 概念说明

interprocess 用**三种类型**来表示「一个安全描述符」，分别对应 Rust 的三种内存关系，这是本模块设计的精髓：

| 类型 | 所有权 | 可变性 | 是否要求 absolute 格式 | 对应 Rust 概念 |
| --- | --- | --- | --- | --- |
| `SecurityDescriptor` | **拥有**（drop 释放 ACL/SID） | 不可变（内部无内部可变性） | 是 | `T`（拥有值） |
| `BorrowedSecurityDescriptor<'a>` | 借用 | 不可变 | 否 | `&'a T` |
| `MutBorrowedSecurityDescriptor<'a>` | 借用 | 可变 | 是 | `&'a mut T` |

贯穿三者的统一抽象是 `unsafe trait AsSecurityDescriptor`——它只做一件事：返回一个能喂给 Win32 函数的 `*const c_void`。这是一个典型的**能力 trait**：实现它等于承诺「我背后藏着一个合法的 SD」。

#### 4.2.2 核心流程

围绕「拥有型」的生命周期，核心流程如下：

1. **创建**：`SecurityDescriptor::new()` 调 `InitializeSecurityDescriptor`，得到一个 absolute 格式、内容为空的 SD。
2. **配置**：通过 `AsSecurityDescriptorMutExt` 的 `set_dacl`/`set_owner` 等方法填入 ACL/SID。
3. **借用**：在需要传给 Win32 但不放弃所有权时，用 `borrow()` 得到一个带生命周期的 `BorrowedSecurityDescriptor`。
4. **释放**：`Drop` 时调用 `free_contents()`，逐个 `LocalFree` 掉 DACL/SACL/owner/group 指向的本地堆内存。
5. **深拷贝**：`TryClone::try_clone()` 递归复制所有 ACL 与 SID，产生一个独立拥有的新 SD。

#### 4.2.3 源码精读

拥有型本体只是一个对 `SECURITY_DESCRIPTOR` 的 `#[repr(C)]` newtype，但因为其中的 ACL/SID 是裸指针、需要跨线程只读共享，故手工标注 `Sync`/`Send`：

[src/os/windows/security_descriptor/owned.rs:24-37](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/owned.rs#L24-L37) — `SecurityDescriptor(SECURITY_DESCRIPTOR)` 定义，以及 `unsafe impl Sync/Send` 与两个 `AsSecurityDescriptor*` 实现（`as_sd()`/`as_sd_mut()` 只是取 `&self`/`&mut self` 的地址）。

构造器 `new()` 是最干净的入口，展示了 interprocess 统一的「`true_or_errno` 把 BOOL 翻译成 `io::Result`」模式：

[src/os/windows/security_descriptor/owned.rs:40-50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/owned.rs#L40-L50) — 调用 `InitializeSecurityDescriptor`，成功时用 `from_owned` 包成拥有型。

`from_owned` 是一道**安全闸门**：它在 `debug_assert!` 里校验「不是 self-relative」「`IsValidSecurityDescriptor` 为真」，从而把「absolute 且有效」这个不变式强制钉死：

[src/os/windows/security_descriptor/owned.rs:68-84](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/owned.rs#L68-L84) — `unsafe fn from_owned`：先 `control_and_revision` 校验非 self-relative，再 `validate()` 校验有效，最后包成 `Self`。

借用型 `BorrowedSecurityDescriptor<'a>` 用 `NonNull<c_void> + PhantomData<&'a SECURITY_DESCRIPTOR>` 表达一个带生命周期的不可变借用，`#[repr(transparent)]`、`Copy`，且**不要求** absolute 格式（只读、不改，所以 self-relative 也安全）：

[src/os/windows/security_descriptor/borrowed.rs:19-29](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/borrowed.rs#L19-L29) — `BorrowedSecurityDescriptor` 定义及其 `AsSecurityDescriptor` 实现。

三者的统一契约写在 `AsSecurityDescriptor` 的 safety 段里，这是阅读本模块必须理解的三条不变式：

[src/os/windows/security_descriptor/as_security_descriptor.rs:20-36](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/as_security_descriptor.rs#L20-L36) — `unsafe trait AsSecurityDescriptor`：实现者必须保证 SD 内部指针有效、不被可变别名、且 `IsValidSecurityDescriptor()` 返回真。

> 关键点：这是一个 `unsafe trait`——不是「调用它不安全」，而是「实现它不安全」。interprocess 把它设为 unsafe，强制只有满足上述三条的类型才能实现，从而让所有消费方（如 `create_security_attributes`）可以放心地把裸指针递给 Win32。

`Drop` 走 `free_contents()`，它复用了 ext trait 的 `remove_acls`/`remove_sids`（先 unset 再 free），保证「即使释放失败，SD 本身仍是一个合法的空 SD」：

[src/os/windows/security_descriptor/owned.rs:118-122](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/owned.rs#L118-L122) — `Drop for SecurityDescriptor` 调 `free_contents()`，释放失败时只 `debug_expect` 而不 panic（非致命）。

#### 4.2.4 代码实践

**实践目标**：跟踪拥有型 SD 从创建到释放的完整内存链，理解「按值拥有 ACL/SID」的真实含义。

**操作步骤**：

1. 从 `SecurityDescriptor::new()` 出发，在 `owned.rs` / `ext.rs` / `c_wrappers.rs` / `try_clone.rs` 之间跳转，画出一条「`drop` → `free_contents` → `remove_acls`+`remove_sids` → `LocalFree`」的调用链。
2. 阅读 `try_clone.rs` 顶部的 `clone()` 函数，注意它在复制 DACL 时**特判了 null ACL**：

   [src/os/windows/security_descriptor/try_clone.rs:43-50](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/try_clone.rs#L43-L50) — 当原 DACL 指针为 null（null DACL）时，用 `set_dacl(ptr::null_mut(), dfl)` 在新 SD 上**复刻同样的 null DACL**，而非把它当成「无 ACL」。这正是 4.1 节那条语义区别在克隆路径上的体现。

**需要观察的现象 / 预期结果**：你能解释「为什么 `try_clone` 一个 null DACL 的 SD，结果仍是全开而非默认安全」。预期：因为克隆忠实保留了 present=true、ptr=null 的语义。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BorrowedSecurityDescriptor` 不要求 SD 是 absolute 格式，而 `SecurityDescriptor` 与 `MutBorrowedSecurityDescriptor` 都要求？

> **参考答案**：不可变借用只读不改，self-relative（连续内存、指针是偏移量）也能安全读；而拥有型和可变借用要在其上调用 `SetSecurityDescriptor*`，这些写入操作要求 absolute 格式（指针是真实地址）。

**练习 2**：`AsSecurityDescriptor` 是 `unsafe trait`，这是否意味着调用 `as_sd()` 是 unsafe 的？

> **参考答案**：不是。`unsafe trait` 限制的是「谁能实现它」，`as_sd()` 本身是安全方法。任何能拿到 `impl AsSecurityDescriptor` 的代码，都已在编译期获得「其背后 SD 合法」的保证。

### 4.3 从 ListenerOptions 到 CreateNamedPipeW：把 SD 接进 named pipe

#### 4.3.1 概念说明

named pipe 是可安全对象，`CreateNamedPipeW` 的最后一个参数就是指向 `SECURITY_ATTRIBUTES`（内含 SD 指针）的指针。interprocess 没有在跨平台的 `ListenerOptions` 上直接开一个 `security_descriptor` 的公开 setter——因为 SD 是 Windows 专有概念——而是用一个 **Windows 专有的扩展 trait `ListenerOptionsExt`** 来追加这个能力。这样既保持公共 API 跨平台一致，又让 Windows 用户能拿到原生权限控制。

#### 4.3.2 核心流程

设置并使用一个 SD 的完整调用链如下（每一跳都标注了文件）：

```
ListenerOptions::new()
  └─ .security_descriptor(sd)          # ListenerOptionsExt 方法
     └─ 写入 pub(crate) 字段 options.security_descriptor
        └─ create_sync() / create_sync_as::<Listener>()
           └─ Listener::from_options(options)         # Windows 后端
              └─ impl_options.security_descriptor = options.security_descriptor
                 └─ PipeListenerOptions::create() → _create() → create_instance()
                    └─ create_security_attributes(sd.borrow(), inheritable)
                       └─ sd.write_to_security_attributes(&mut SECURITY_ATTRIBUTES)
                          └─ CreateNamedPipeW(..., &SECURITY_ATTRIBUTES)
```

公共 `ListenerOptions` 字段是 `pub(crate)`，故外部用户只能通过扩展 trait 的 `.security_descriptor()` 方法写入；这条「字段私有、trait 暴露」的设计是理解整条链的钥匙。

#### 4.3.3 源码精读

公共 `ListenerOptions` 里，`security_descriptor` 是 `#[cfg(windows)]` 的私有字段，默认 `None`：

[src/local_socket/listener/options.rs:17-26](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L17-L26) — 结构体定义：Unix 上根本没有这个字段，`security_descriptor: Option<SecurityDescriptor>` 只在 Windows 编译。

真正的 setter 在 Windows 专有扩展 trait 上，必须 `use` 它才能调用：

[src/os/windows/local_socket.rs:17-29](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/local_socket.rs#L17-L29) — `pub trait ListenerOptionsExt: Sized + Sealed` 声明 `fn security_descriptor(self, sd: SecurityDescriptor) -> Self`；实现里直接 `self.security_descriptor = Some(sd)`。注意它是 `Sealed`（封印）的，外部无法为别的类型实现。

> 这正是你调用 `.security_descriptor()` 时编译器有时报「method not found」的原因——必须先把 `ListenerOptionsExt` 引入作用域。它**不在**跨平台的 `local_socket::prelude` 里。

Windows 后端的 `from_options` 把公共选项翻译成原生 `PipeListenerOptions`，只搬运 `path`、非阻塞 accept 维度与 `security_descriptor` 三项：

[src/os/windows/named_pipe/local_socket/listener.rs:30-42](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/local_socket/listener.rs#L30-L42) — 第 39 行 `impl_options.security_descriptor = options.security_descriptor;` 完成搬运（这是按值移动一个 `Option<SecurityDescriptor>`）。

原生层在真正 `CreateNamedPipeW` 之前，用 `create_security_attributes` 把拥有型 SD「借」出来塞进 `SECURITY_ATTRIBUTES`：

[src/os/windows/named_pipe/listener/create_instance.rs:51-79](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/named_pipe/listener/create_instance.rs#L51-L79) — `create_security_attributes(self.security_descriptor.as_ref().map(|sd| sd.borrow()), self.inheritable)`，再把 `&sa` 作为最后一个参数传给 `CreateNamedPipeW`。

`create_security_attributes` 自身是模块根里的 `pub(super)` 函数：零初始化一个 `SECURITY_ATTRIBUTES`，若有 SD 就用 `write_to_security_attributes` 写指针，再填 `nLength` 与 `bInheritHandle`：

[src/os/windows/security_descriptor.rs:37-48](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor.rs#L37-L48) — 组装 `SECURITY_ATTRIBUTES`。注意 `bInheritHandle` 由 `inheritable` 决定（named pipe 句柄一般不继承，故 `PipeListenerOptions::inheritable` 默认 `false`）。

而 `write_to_security_attributes` 只是 `AsSecurityDescriptorExt` 的一行方法，把 `self.as_sd()` 写进 `lpSecurityDescriptor` 字段——SD 本身**不被** `SECURITY_ATTRIBUTES` 拥有，借用关系由 Rust 生命周期保证：

[src/os/windows/security_descriptor/ext.rs:176-178](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/ext.rs#L176-L178) — `attributes.lpSecurityDescriptor = self.as_sd().cast_mut()`，文档明确说明 `SECURITY_ATTRIBUTES` 不延长 SD 的生命周期。

#### 4.3.4 代码实践

**实践目标**：参考 `tests/os/windows/local_socket_security_descriptor/null_dacl.rs`，写一个最小的「带 null DACL 的 local socket 监听器」，并验证任意客户端都能连上。

> 以下为**示例代码**（非项目原有文件），且仅在 **Windows** 上可编译运行；在 Linux/macOS 上应整体放进 `#[cfg(windows)]` 或独立项目。运行结果需在 Windows 本地验证。

```rust
// Cargo.toml 需启用对应依赖；示例代码，非仓库原有
use interprocess::{
    local_socket::{prelude::*, ListenerOptions, Stream},
    os::windows::{
        local_socket::ListenerOptionsExt,           // .security_descriptor() 的来源
        security_descriptor::{
            AsSecurityDescriptorMutExt,             // set_dacl 的来源
            SecurityDescriptor,
        },
    },
    TryClone,                                        // try_clone 的来源
};
use std::ptr;

fn main() -> std::io::Result<()> {
    let mut sd = SecurityDescriptor::new()?;         // 得到一个 absolute、空内容的 SD
    // 注意：set_dacl 是 #[doc(hidden)] 的 unsafe 方法，因为它接收裸 *mut ACL 并假设所有权。
    unsafe {
        sd.set_dacl(ptr::null_mut(), false)?;        // present=true、ptr=null → null DACL（全开！）
    }

    let name = "example-null-dacl.sock".to_ns_name()?;
    let _listener = ListenerOptions::new()
        .name(&name)
        .security_descriptor(sd.try_clone()?)        // 经扩展 trait 注入 SD
        .create_sync()?;

    // 任何本机进程现在都能连接这个管道（null DACL 的后果）
    let _client = Stream::connect(name)?;
    Ok(())
}
```

**操作步骤**：

1. 在 Windows 上用 `cargo`（启用 `tokio` 与否均可，本例为同步）编译运行上述程序。
2. 对照 `tests/os/windows/local_socket_security_descriptor/null_dacl.rs:14-28` 确认每一步：
   - [null_dacl.rs:16-18](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/os/windows/local_socket_security_descriptor/null_dacl.rs#L16-L18) — 构造 SD 并 `set_dacl(ptr::null_mut(), false)`；
   - [null_dacl.rs:21-25](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/os/windows/local_socket_security_descriptor/null_dacl.rs#L21-L25) — `.security_descriptor(sd.try_clone()?).create_sync()`。

**需要观察的现象 / 预期结果**：客户端连接成功（`Stream::connect` 返回 `Ok`）。**待本地验证**：若把 `set_dacl(null)` 一行删掉（即保持 absent DACL），连接行为应仍成功（默认安全通常允许本机同令牌）；若改成一个只允许特定 SID 的正常 DACL，则未被授权的客户端连接会被拒绝。

> 警告：null DACL 等同于「任何本机进程都可全权限访问该管道」，仅在受控测试环境使用，**不要**用于生产鉴权。

#### 4.3.5 小练习与答案

**练习 1**：为什么 interprocess 不把 `.security_descriptor()` 直接放进跨平台的 `prelude`？

> **参考答案**：因为 SD 是 Windows 专有概念，放进跨平台 prelude 会让 Unix 用户看到无法编译的类型。放在 `os::windows::local_socket::ListenerOptionsExt` 上，由 `#[cfg(windows)]` 隐式门控，Unix 上该 trait 根本不编译。

**练习 2**：`impl_options.security_descriptor = options.security_descriptor;` 这一行是拷贝、移动还是克隆？

> **参考答案**：是**按值移动**（move）一个 `Option<SecurityDescriptor>`。`SecurityDescriptor` 没有 `Clone`（克隆可能失败），故 `ListenerOptions` 在 `try_clone` 时才对它调用 `TryClone::try_clone`，见 [options.rs:43-60](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/local_socket/listener/options.rs#L43-L60)。

### 4.4 c_wrappers：unsafe FFI 封装与错误转换

#### 4.4.1 概念说明

SD 模块所有真正的 Win32 调用都被收拢进 `c_wrappers.rs`，每个都是 `unsafe fn`。这一层有两个反复出现的工具：

- **`OrErrno` / `BoolExt`**：把「返回 `BOOL`、失败靠 `GetLastError()` 体现」的 Win32 风格，翻译成 Rust 的 `io::Result`。例如 `ret.true_or_errno(|| ok_val)` 表示「`ret` 为真则返回 `ok_val`，否则返回 `last_os_error()`」。
- **`LocalBox<T>`**：一个轻量的「本地堆」智能指针，对应 `LocalAlloc`/`LocalFree`。Win32 的 ACL/SID 规定用本地堆分配，interprocess 用 `LocalBox` 守住「必须 `LocalFree`」的契约。

这一层也是 crate 安全策略的体现：`unsafe_op_in_unsafe_fn = forbid` 级别 lint 要求**每一个** unsafe 操作都包在显式 `unsafe { }` 块里。

#### 4.4.2 核心流程

c_wrappers 的函数遵循统一模式：

1. 准备若干 `mut` 输出变量（多为 `ptr::null_mut()` 或 `zeroed()`）；
2. 用 `unsafe { ... }` 调用 Win32 函数；
3. 用 `true_or_errno` / `true_val_or_errno` 把返回的 `BOOL` 折叠成 `io::Result`。

读、写、释放、序列化四类操作各有一个通用小帮手：`acl`/`sid`（读）、`set_acl`/`set_sid`（写）、`free_acl`/`free_sid`（释放）、`serialize`/`deserialize`（字符串格式互换）。

#### 4.4.3 源码精读

`set_acl` 是最典型的封装，前文 4.1.3 已引用；这里看一个「读」操作 `acl`，体会「输出指针 + exists 标志」的模式：

[src/os/windows/security_descriptor/c_wrappers.rs:30-44](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/c_wrappers.rs#L30-L44) — `acl()` 接收一个 Win32 getter 函数指针，用 `exists` 标志区分「有 ACL」与「无 ACL」，再 `true_or_errno` 翻译错误。这正是 `dacl()`/`sacl()` 背后的统一实现。

序列化与反序列化对称成对，是「字符串格式（SDDL）↔ 二进制 SD」互转的入口：

[src/os/windows/security_descriptor/c_wrappers.rs:111-143](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/c_wrappers.rs#L111-L143) — `serialize` 调 `ConvertSecurityDescriptorToStringSecurityDescriptorW` 并把结果包进 `LocalBox`；`deserialize` 反向调用，结果同样由 `LocalBox` 拥有。

`LocalBox` 本体定义在 `try_clone.rs`，drop 时 `LocalFree`：

[src/os/windows/security_descriptor/try_clone.rs:77-103](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/try_clone.rs#L77-L103) — `LocalBox<T>`：`allocate` 用 `LocalAlloc(LMEM_FIXED, sz)`，`Drop` 用 `LocalFree`，保证与 Win32 的内存约定一致。它是 `pub(crate)`，故只有同 crate 的测试（见 sd_graft.rs）能直接用。

> 值得对比的细节：`free_acl` 对 null 指针直接 `LocalFree`（`is_null().true_val_or_errno`），而 `free_sid` 先判 null 提前返回——因为 null ACL 是「合法值」（不能 free），null PSID 是「缺席哨兵」（无需 free）。两者对 null 的不同处理，再次映射了 4.1 节的语义区别。

#### 4.4.4 代码实践

**实践目标**：精读一个 `unsafe` 封装，说清「前置条件、错误转换、lint 合规」三件事。

**操作步骤**：

1. 选 [c_wrappers.rs:55-66](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/c_wrappers.rs#L55-L66) 的 `set_acl`。
2. 逐行回答：
   - **前置条件**：调用方传入的 `f` 必须是合法的 `Set*SecurityDescriptor*Acl` 函数，`sd` 必须指向合法 SD（由 `AsSecurityDescriptor` 的 safety 契约保证）。
   - **错误转换**：`f(...).true_val_or_errno(())` 把 `BOOL` 翻成 `io::Result<()>`。
   - **lint 合规**：整个 Win32 调用包在 `unsafe { }` 中，满足 `unsafe_op_in_unsafe_fn = forbid`。
3. 对比同文件 `free_acl`（[L97-99](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/c_wrappers.rs#L97-L99)）与 `free_sid`（[L100-109](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/src/os/windows/security_descriptor/c_wrappers.rs#L100-L109)）对 null 输入的处理差异。

**需要观察的现象 / 预期结果**：你能用自己的话讲清「为什么这两个 free 函数对 null 的处理不同」。

#### 4.4.5 小练习与答案

**练习 1**：`true_or_errno(|| x)` 与 `true_val_or_errno(x)` 有何区别？

> **参考答案**：`true_or_errno` 接收一个闭包，仅在成功时**惰性求值**产出成功值（适合代价较高的成功值构造）；`true_val_or_errno` 接收一个已求值的成功值。二者都在 `BOOL` 为假时返回 `Err(last_os_error())`。

**练习 2**：为什么 `LocalBox` 是 `pub(crate)` 而非 `pub`？

> **参考答案**：它是对 Win32 本地堆的内部封装，不属于 interprocess 的公共 API 契约；公开它会增加维护负担且对用户无价值。模块文档（security_descriptor.rs:1-8）也强调 interprocess 刻意不暴露这类底层细节，鼓励用户用别的 crate 处理 SD。

## 5. 综合实践

**任务**：参考 `tests/os/windows/local_socket_security_descriptor/` 下的两个测试，写一个完整的 demo，对比「null DACL」与「从可执行文件复制的真实 SD」两种权限策略对客户端访问的影响。

**分步要求**：

1. **null DACL 路径**（参考 [null_dacl.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/os/windows/local_socket_security_descriptor/null_dacl.rs)）：
   - `SecurityDescriptor::new()` + `unsafe { sd.set_dacl(ptr::null_mut(), false)?; }`；
   - 用 `ListenerOptionsExt::security_descriptor` 挂到监听器；
   - 启动一个客户端连接，确认能连上。
2. **SD 嫁接路径**（参考 [sd_graft.rs](https://github.com/kotauskas/interprocess/blob/ecb9daf2ee7cf5fd5ea4ea6d99e937232c4f38c7/tests/os/windows/local_socket_security_descriptor/sd_graft.rs)，该测试用 `GetSecurityInfo` 取自身可执行文件的 SD，`to_owned_sd()` 复制后挂到监听器，再用 `serialize` 把新旧 SD 都打成 SDDL 字符串比对）：
   - 用 `GetSecurityInfo` 取得某文件对象的 SD，`to_owned_sd()` 转成拥有型；
   - 挂到监听器后，用 `AsSecurityDescriptorExt::serialize` 把「预期 SD」与「监听器实际 SD」序列化成 SDDL，比对两者的非 ACL 部分与 ACE 数量（对应 sd_graft.rs 里的 `ensure_equal_non_acl_part` 与 `ensure_equal_number_of_opening_parentheses`）。

**需要观察的现象 / 预期结果**：

- null DACL 路径下，任意本机客户端都能连接。
- 嫁接路径下，序列化出的两条 SDDL 字符串在 owner/group 与 ACE 数量上应一致（说明 SD 被忠实地应用到了管道对象上）。

> 注意：sd_graft 测试带 `#[cfg(not(ci))]`（CI 环境跳过），null_dacl 测试不带此门控。两条路径都**仅在 Windows 可运行**，结果需在 Windows 本地验证。本综合实践为源码阅读 + 最小实现型任务，未假定已实际运行。

**反思题**：如果你的服务只想让「与本服务同一用户」的进程连接，应该用 null DACL、absent DACL，还是构造一个允许当前用户 SID 的 DACL？为什么？

> 参考方向：null DACL 过宽（全开）；absent DACL 依赖默认策略、不够显式；正确做法是构造一个允许当前用户 SID（可通过 `GetSecurityInfo` 取自身令牌的 owner SID）所需权限的 DACL，确保「显式、最小权限」。

## 6. 本讲小结

- 安全描述符由 owner/group SID、DACL、SACL 加控制位组成；named pipe 作为可安全对象，其「谁能连接」由 DACL 决定。
- **null DACL（present=true、指针为空）授予所有人全部权限，与 absent DACL（present=false）截然相反**——这是本模块反复强调的头号陷阱，`unset_*` 与 `set_*(null)` 两个方法专门区分它们。
- interprocess 用三种类型表示 SD：拥有型 `SecurityDescriptor`（drop 释放内容）、`BorrowedSecurityDescriptor`（共享借用、不要求 absolute）、`MutBorrowedSecurityDescriptor`（可变借用、要求 absolute），统一于 `unsafe trait AsSecurityDescriptor` 的三条安全契约。
- SD 经 **Windows 专有扩展 trait `ListenerOptionsExt`** 注入：`.security_descriptor(sd)` 写入 `pub(crate)` 字段 → 后端 `from_options` 搬运 → `create_security_attributes` 借出 → `CreateNamedPipeW` 的 `SECURITY_ATTRIBUTES`。
- `try_clone` 忠实复刻 null DACL；`LocalBox` 守住「本地堆分配/释放」契约；`c_wrappers` 用 `OrErrno` 把 BOOL 风格翻译成 `io::Result`，并满足 `unsafe_op_in_unsafe_fn = forbid`。
- 外部用户只能用 `pub` 项（`SecurityDescriptor`、两个 ext trait、`ListenerOptionsExt`）；`LocalBox` 等 `pub(crate)` 项只有同 crate 的测试能触及（tests 通过 `#[path]` 挂进库 crate）。

## 7. 下一步学习建议

- **u8-l1 / u8-l2**：Windows named pipe 还有一套与 SD 无关但同样精巧的内部机制——drop 时延迟刷新的 `linger_pool`、惰性堆分配的 `maybe_arc`。本讲结束时 `create_instance` 返回的句柄在 drop 后的去向，正是由它们决定的。
- **u9-l1（unsafe FFI 封装层）**：本讲 4.4 的 `OrErrno`、`LocalBox`、`unsafe_op_in_unsafe_fn = forbid` 在全库的 `c_wrappers` 家族中是一致模式，下一单元会系统讲解。
- **延伸阅读**：Microsoft Learn 上《[Creating a Security Descriptor for a New Object in C++](https://learn.microsoft.com/en-us/windows/win32/secauthz/creating-a-security-descriptor-for-a-new-object-in-c--)》与《[Security Descriptor String Format](https://learn.microsoft.com/en-us/windows/win32/secauthz/security-descriptor-string-format)》，对照 `owned.rs` 文档注释里给出的链接，理解 SDDL 字符串（如 `D:(A;;GA;;;WD)`）如何对应一个 DACL。
