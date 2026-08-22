# u2-l4 系统集成服务：剪贴板、URL、文件对话框与凭据存储

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `read_from_clipboard`（同步）与 `read_from_clipboard_async`（异步）各自适合什么场景，为什么浏览器平台只有后者能用。
2. 理解 Linux 的「主选区（primary selection）」和普通剪贴板的区别，以及 `read_from_primary` 为什么是只存在于 Linux/FreeBSD 上的 `#[cfg]` 门控方法。
3. 描述 `write_credentials` / `read_credentials` / `delete_credentials` 凭据三件套在 macOS、Windows、Linux、Web 四个平台上分别落到哪个系统服务（Keychain / Credential Manager / Secret Service / 无）。
4. 能对照真实源码说出一次「写入剪贴板 → 读回校验」在你当前操作系统上的完整调用路径。

本讲是 u2 单元的最后一讲，继续沿着 [u2-l1](u2-l1-platform-trait-tour.md) 建立的「Platform trait 八大方法分组」往下钻，这次覆盖其中的「系统集成」组。

## 2. 前置知识

### 2.1 剪贴板不只是「一段文本」

现代操作系统的剪贴板是一个**多格式键值存储**：同一次复制可以同时携带纯文本、图片、文件路径等多种格式，粘贴方按自己的能力挑一个用。macOS 用 `NSPasteboard` 的「类型 → 数据」模型，Windows 用「剪贴板格式号（如 `CF_UNICODETEXT`、`CF_HDROP`）」，Linux X11 用「目标原子（target atom）」，Wayland 用「MIME 类型」。GPUI 把这些统一抽象成 `ClipboardItem`。

还有两个平台特有的剪贴板变体：

- **Linux 主选区（primary selection）**：X11/Wayland 传统上有两块剪贴板——普通剪贴板由 Ctrl+C/V 使用，主选区则随「鼠标选中一段文字」自动更新、鼠标中键粘贴。终端里的选中即复制就是这个机制。
- **macOS 查找板（Find pasteboard）**：一个系统级约定粘贴板，专门用来在多个应用之间共享「当前搜索关键词」，Cmd+E 填入、Cmd+F 读取。

### 2.2 凭据存储（credentials）

「凭据」指 username + password 形式的敏感数据。三个桌面平台都有系统级加密存储：

| 平台 | 系统服务 | GPUI 使用的 API |
|---|---|---|
| macOS | 钥匙串 Keychain | Security.framework 的 `SecItemAdd` / `SecItemUpdate` / `SecItemCopyMatching` / `SecItemDelete` |
| Windows | 凭据管理器 Credential Manager | `CredWriteW` / `CredReadW` / `CredDeleteW` |
| Linux | Secret Service（GNOME Keyring / KWallet 后端） | `oo7` crate 的 `Keyring` |
| Web | 无 | 直接返回错误 |

### 2.3 xdg-desktop-portal 与 ashpd

Linux 桌面的文件选择器、设置读取等系统对话框走 [xdg-desktop-portal](https://flatpak.github.io/xdg-desktop-portal/)——一个 DBus 服务，沙箱应用（Flatpak 等）和普通应用都通过它请求「系统能力」。`ashpd` 是它的 Rust 客户端 crate（ASHPD = A SHell Portal Desktop）。Zed 的 Linux 文件对话框不自己画 UI，而是发 portal 请求，由桌面环境（GNOME/KDE 等）提供的 portal 实现弹出真正的对话框。

### 2.4 复习：Task 与 oneshot::Receiver

本讲大量出现两种异步返回值（u2-l1 已总结过）：

- `Task<T>`：GPUI 自己的 future 句柄，可 `await`、可 `detach`、可存储。
- `oneshot::Receiver<T>`：一次性通道接收端，适合「平台在后台线程弹对话框，完成后回传结果」的模式。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [../gpui/src/platform.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs) | 契约层：`Platform` trait 的系统集成方法、`ClipboardItem` / `PathPromptOptions` / `ClipboardReadError` 数据模型 |
| [../gpui/src/app.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs) | `App` 对这些平台方法的逐一转发，应用层代码日常调用的入口 |
| [../gpui_macos/src/pasteboard.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/pasteboard.rs) | macOS `NSPasteboard` 封装：`Pasteboard::read` / `write`，含文件、文本、图像与私有 metadata |
| [../gpui_macos/src/platform.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs) | `MacPlatform` 的 `open_url`、`prompt_for_paths`（NSOpenPanel）、Keychain 凭据三件套 |
| [../gpui_windows/src/clipboard.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/clipboard.rs) | Windows 剪贴板自由函数实现：`ClipboardGuard`、格式枚举、自定义格式注册 |
| [../gpui_windows/src/platform.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs) | `WindowsPlatform` 的剪贴板转发与 Credential Manager 凭据三件套 |
| [../gpui_linux/src/linux/platform.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs) | `LinuxPlatform` 外壳：portal 文件对话框、oo7 凭据、剪贴板/主选区转发到 `LinuxClient` 后端 |
| [../gpui_linux/src/linux/wayland/client.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/client.rs) | Wayland 后端：主选区/剪贴板的 data-offer 协议实现 |
| [../gpui_linux/src/linux/x11/client.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/x11/client.rs) | X11 后端：`ClipboardKind::Primary` / `Clipboard` 双剪贴板读写 |
| [../gpui_linux/src/linux/headless/client.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/headless/client.rs) | headless 后端：所有系统能力静默空操作 |
| [../gpui_linux/src/linux/xdg_desktop_portal.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/xdg_desktop_portal.rs) | ashpd 的另一个用途：订阅 settings portal（外观/光标主题）。注意它**不**是文件选择器代码 |
| [../gpui_web/src/platform.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_web/src/platform.rs) | Web 后端：`read_from_clipboard_async` 的真实实现（navigator.clipboard API） |
| [../gpui/examples/input.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/input.rs) | 官方示例：copy/cut/paste 动作处理器中剪贴板的标准用法 |

## 4. 核心概念与源码讲解

### 4.1 剪贴板契约与数据模型：`ClipboardItem` 的多格式抽象

#### 4.1.1 概念说明

`Platform` trait 只要求两个剪贴板方法：`read_from_clipboard`（同步读，返回 `Option<ClipboardItem>`）和 `write_to_clipboard`（同步写）。一切复杂性都藏在 `ClipboardItem` 这个数据模型里——它把 macOS 的类型键值对、Windows 的格式号、Wayland 的 MIME 类型统一成「若干个 `ClipboardEntry`」。

一个容易被忽略的设计点：**GPUI 在剪贴板里私藏了 metadata**。Zed 复制代码时会附带「这是哪种语法高亮的代码」之类的元数据，这样在 Zed 内部粘贴能恢复高亮，粘贴到外部应用则自动退化为纯文本。元数据写在自定义格式里（macOS 是自定义 UTI，Windows 是 `RegisterClipboardFormatW` 注册的私有格式），并附带一段文本哈希用于校验「元数据确实属于当前这段文本」。

#### 4.1.2 核心流程

一次「写 → 读」的完整链路：

```text
应用层: cx.write_to_clipboard(ClipboardItem::new_string(text))
   │
   ▼
App::write_to_clipboard        （gpui/src/app.rs，纯转发）
   │
   ▼
Platform::write_to_clipboard   （trait 契约）
   │
   ├─ macOS:   MacPlatform → state.general_pasteboard.write(item)
   │                    → NSPasteboard: clearContents + setData_forType
   ├─ Windows: WindowsPlatform → clipboard::write_to_clipboard(item)
   │                    → EmptyClipboard + SetClipboardData
   ├─ Linux:   LinuxPlatform → self.inner.write_to_clipboard(item)   （LinuxClient 后端）
   │                    ├─ Wayland: data_device.set_selection(数据源声明各 MIME)
   │                    ├─ X11:     xcb 剪贴板仲裁（ClipboardKind::Clipboard）
   │                    └─ headless: 空操作
   └─ Web:     write_text（fire-and-forget）

读路径对称：App::read_from_clipboard → Platform::read_from_clipboard
   → 各平台把原生数据重新组装成 Option<ClipboardItem>
```

#### 4.1.3 源码精读

**契约定义**（必需方法 + 数据模型）：

[../gpui/src/platform.rs:310-311](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L310-L311) 定义了两个必须实现的同步方法——没有默认实现，任何 `Platform` 实现者都必须给出答案。

[../gpui/src/platform.rs:2304-2354](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L2304-L2354) 是数据模型本体：`ClipboardItem` 只有一个 `entries: Vec<ClipboardEntry>` 字段；`ClipboardEntry` 是三选一——`String(ClipboardString)`、`Image(Image)` 或 `ExternalPaths`（从文件管理器复制来的文件路径列表）。

[../gpui/src/platform.rs:2356-2388](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L2356-L2388) 提供了三个构造器，最常用的是 `ClipboardItem::new_string(text)`；`text()` 方法则把所有字符串条目拼接返回（[../gpui/src/platform.rs:2390-2417](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L2390-L2417)），没有字符串条目时会退而拼接外部文件路径。

**应用层转发**：

[../gpui/src/app.rs:1394-1425](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1394-L1425) 是日常代码实际调用的入口：`App::read_from_clipboard` 和 `App::write_to_clipboard` 都是一行转发。由于 `Context<T>` 会 deref 到 `App`，在 `cx.listener` 回调里直接 `cx.write_to_clipboard(...)` 即可。

**macOS 实现——读取的优先级链**：

[../gpui_macos/src/pasteboard.rs:22-50](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/pasteboard.rs#L22-L50) 定义了 `Pasteboard` 封装，三个构造器分别对应通用粘贴板（`generalPasteboard`）、查找板（`pasteboardWithName(NSPasteboardNameFind)`，见 [../gpui_macos/src/pasteboard.rs:252-256](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/pasteboard.rs#L252-256) 的 extern 声明）和测试用的唯一命名板。

[../gpui_macos/src/pasteboard.rs:52-92](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/pasteboard.rs#L52-L92) 的 `read()` 展示了读取优先级：**文件路径 → 纯文本 → 各种图像格式**。注意第一分支的细节——从文件管理器复制文件时，条目列表同时包含 `ExternalPaths` 和文本表示，这样文本编辑器可以直接把路径粘贴成文字。

[../gpui_macos/src/pasteboard.rs:204-234](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/pasteboard.rs#L204-L234) 的 `write_plaintext` 是私有元数据机制的写入侧：先把文本写进 `NSPasteboardTypeString`，再若有 metadata，则同时写入 `zed-text-hash`（文本哈希，8 字节大端）和 `zed-metadata` 两个自定义 UTI。读取侧 [../gpui_macos/src/pasteboard.rs:114-142](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/pasteboard.rs#L114-L142) 用哈希校验后才采信元数据——防止「复制文本后其他应用只改了文本部分」造成错配。

**Windows 实现——格式枚举与 RAII 守卫**：

[../gpui_windows/src/clipboard.rs:27-47](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/clipboard.rs#L27-L47) 用 `LazyLock` + `RegisterClipboardFormatW` 注册了 `GPUI internal text hash`、`GPUI internal metadata` 等私有格式，与 macOS 的两个自定义 UTI 一一对应。

[../gpui_windows/src/clipboard.rs:92-128](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/clipboard.rs#L92-L128) 的 `read_from_clipboard` 枚举所有格式号，分别匹配 `CF_UNICODETEXT`（文本）、图像格式表、`CF_HDROP`（文件列表），三种各取一条。读到的组合若是空的，会调用 `log_unsupported_clipboard_formats` 把当前剪贴板里的格式名打进日志，方便排查「为什么粘贴不生效」。

[../gpui_windows/src/clipboard.rs:337-357](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/clipboard.rs#L337-L357) 的 `ClipboardGuard` 是典型的 RAII：Windows 要求使用剪贴板前 `OpenClipboard`、用完必须 `CloseClipboard`（否则其他进程会被卡住），守卫在 `Drop` 里保证关闭。元数据校验逻辑在 [../gpui_windows/src/clipboard.rs:229-243](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/clipboard.rs#L229-L243)。

**macOS / Windows 的 trait 接线**：

[../gpui_macos/src/platform.rs:1123-1131](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L1123-L1131) 与 [../gpui_windows/src/platform.rs:824-830](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L824-L830) 都只有两三行——锁住平台状态，委托给上面读过的实现。

#### 4.1.4 代码实践

**实践目标**：用官方 `input` 示例验证「写入 → 读回」链路，并定位每一层源码。

**操作步骤**：

1. 打开 [../gpui/examples/input.rs:144-163](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/input.rs#L144-L163)，阅读三个动作处理器：`paste` 用 `cx.read_from_clipboard().and_then(|item| item.text())`；`copy`/`cut` 用 `cx.write_to_clipboard(ClipboardItem::new_string(...))`。
2. 在仓库根目录运行示例（待本地验证，Linux 上需按 u1-l3 的说明启用 wayland 或 x11 feature）：

   ```bash
   cargo run -p gpui --example input --features gpui/wayland
   ```

3. 在输入框里选中一段文字，按 Cmd/Ctrl+C，再到系统其他应用（如文本编辑器）粘贴，确认文本带出去了。
4. 用 rust-analyzer 从 `cx.write_to_clipboard` 开始逐层「Go to Definition」，把经过的每一层（App 转发 → trait 方法 → 平台实现）的文件与行号记成笔记。

**需要观察的现象**：Zed 内粘贴与外部应用粘贴行为一致（都得到纯文本）；若在两个 Zed 窗口间复制粘贴代码，高亮信息可能保留——这正是私有元数据格式在起作用。

**预期结果**：得到一张「调用层级 × 三个平台」的路径表，本讲 4.1.2 的流程图可作为对照答案。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ClipboardItem` 用 `Vec<ClipboardEntry>` 而不是一个 `enum ClipboardContent` 单值？

**答案**：因为真实剪贴板本来就是多格式的：macOS 一次复制可以同时携带 `NSPasteboardTypeString`、`NSPasteboardTypePNG` 和 `NSFilenamesPboardType`；Windows 枚举格式号时也可能同时看到 `CF_UNICODETEXT` 和 `CF_HDROP`。用 Vec 让「文件路径 + 文本表示」这类组合能原样表达（见 [../gpui_macos/src/pasteboard.rs:63-73](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/pasteboard.rs#L63-L73) 的双条目返回）。单值 enum 会强迫实现方丢掉信息。

**练习 2**：macOS 写入元数据时为什么要同时写一个文本哈希？读回时不校验哈希会发生什么？

**答案**：剪贴板是共享资源，其他应用可能拿到所有权后只替换文本、保留（或清空）GPUI 的自定义类型数据。哈希把「元数据属于哪段文本」绑定起来；[读取侧](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/pasteboard.rs#L126-L138)发现哈希与当前文本不匹配就丢弃元数据。不校验的话，可能把 A 代码的语法高亮元数据错误地贴到 B 文本上。

### 4.2 平台专属剪贴板：Linux 主选区与 macOS 查找板

#### 4.2.1 概念说明

普通剪贴板是所有平台共有的，但有两个「额外剪贴板」只属于特定平台：

- **主选区**：X11 的历史传统，Wayland 以 `zwp_primary_selection_unstable_v1` 协议延续。它和 Ctrl+C/V 剪贴板完全独立——鼠标选中即写入，中键即粘贴。Zed 编辑器在 Linux 上要支持这两种交互，所以 `Platform` trait 里专门有两个 Linux/FreeBSD 专属方法。
- **查找板**：macOS 的系统级「当前搜索词」共享通道，Safari、Finder 等都遵守。

这类「只有部分平台有」的能力，在 gpui 里的表达方式是 `#[cfg]` 门控的 trait 方法——这是契约层条件编译的典型样本，和 u1-l4 里 `current_platform` 的四段 `#[cfg]` 是同一思想在不同粒度上的应用。

#### 4.2.2 核心流程

Linux 上一次主选区写入（Wayland 后端）的流程：

```text
LinuxPlatform::write_to_primary(item)          （外壳转发）
   ▼
LinuxClient::write_to_primary                  （后端契约，platform.rs:90）
   ▼ Wayland 后端：
检查 primary_selection_manager 与 primary_selection 全局对象是否存在
   ▼
要求窗口当前持有键盘或鼠标焦点（协议规定选区所有权属于活动窗口）
   ▼
state.clipboard.set_primary(item)              （本地缓存，供本进程读回）
   ▼
取得「选区序列号」serial（最近一次按键/按下事件的序列号，可为空则放弃）
   ▼
创建 data_source，声明 TEXT_MIME_TYPES + 自描述 MIME
   ▼
primary_selection.set_selection(Some(&data_source), serial)
```

注意「serial 为空就放弃」这一步：Wayland 协议要求设置选区时携带引发它的输入事件序列号，若应用从未收到过任何按键/按下事件（比如刚启动），声明所有权是非法的，代码选择记 warning 后跳过。

#### 4.2.3 源码精读

**契约层的 cfg 门控**：

[../gpui/src/platform.rs:324-332](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L324-L332) 是本讲的第一个关键片段：`read_from_primary` / `write_to_primary` 只在 `any(target_os = "linux", target_os = "freebsd")` 下存在于 trait 中，`read_from_find_pasteboard` / `write_to_find_pasteboard` 只在 macOS 下存在。编译到 Windows 时这四个方法**在类型系统层面就不存在**，比运行时返回 `None` 更强的保证。

[../gpui/src/app.rs:1427-1459](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1427-L1459) 的 `App` 转发方法带同样的 cfg 门和文档注释——应用代码需要用 `#[cfg(any(target_os = "linux", target_os = "freebsd"))]` 包裹调用点。

**Linux 三后端的差异**：

[../gpui_linux/src/linux/platform.rs:88-93](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L88-L93) 是 `LinuxClient` 后端契约中剪贴板相关的四个方法（`write_to_primary` / `write_to_clipboard` / `read_from_primary` / `read_from_clipboard`），加上 `open_uri` 和 `reveal_path`。u1-l4 讲过 `LinuxPlatform` 是外壳、三种后端二次分发——本讲这些方法正好是观察这套结构的最好样本。[../gpui_linux/src/linux/platform.rs:735-749](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L735-L749) 是外壳的四个一行转发。

[../gpui_linux/src/linux/wayland/client.rs:1152-1175](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/client.rs#L1152-L1175) 是 Wayland 的 `write_to_primary`（即 4.2.2 流程图的出处）；[../gpui_linux/src/linux/wayland/client.rs:1177-1209](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/client.rs#L1177-L1209) 是与之平行的 `write_to_clipboard`（用 `data_device.set_selection` 而非 primary_selection 对象）和两个读方法——注意 Wayland 的「读」只读本地缓存，因为 Wayland 协议下读取他人剪贴板是异步协商，实现选择了只在持有所有权时提供数据。

[../gpui_linux/src/linux/x11/client.rs:1739-1790](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/x11/client.rs#L1739-L1790) 是 X11 版本：用同一个 xcb 剪贴板抽象的 `ClipboardKind::Primary` / `ClipboardKind::Clipboard` 两个枚举值区分两块剪贴板。额外亮点是 `read_from_clipboard` 开头的 `is_owner` 检查——如果本进程就是剪贴板当前持有者，直接返回带元数据的缓存副本，绕过 X11 的跨进程协商（X11 下读自己的剪贴板也要走一遍「请求 → 响应」往返）。

[../gpui_linux/src/linux/headless/client.rs:117-131](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/headless/client.rs#L117-L131) 是 headless 版本：写入空操作、读取恒 `None`——无显示环境没有剪贴板可言。

**macOS 查找板**：

[../gpui_macos/src/platform.rs:1133-1141](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L1133-L1141) 显示查找板与通用板共用同一套 `Pasteboard` 封装，只是构造器换成 `Pasteboard::find()`（内部用 [NSPasteboardNameFind](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/pasteboard.rs#L33-L35) 命名板）——这是「同一抽象、不同实例」的复用范例。

#### 4.2.4 代码实践

**实践目标**：在 Linux 上直观区分主选区与普通剪贴板。

**操作步骤**：

1. 准备两个终端，用命令行剪贴板工具观察（工具本身不属于本仓库，X11 下常用 `xclip`，Wayland 下常用 `wl-clipboard`）：

   ```bash
   # X11 会话
   xclip -selection clipboard -o     # 读普通剪贴板
   xclip -selection primary -o       # 读主选区
   # Wayland 会话
   wl-paste                          # 读普通剪贴板
   wl-paste -p                       # 读主选区
   ```

2. 在任意应用里用鼠标选中一段文字（不按 Ctrl+C），执行读主选区命令——应立即输出选中内容；再读普通剪贴板——内容不变。
3. 按 Ctrl+C 后重复两条命令，观察普通剪贴板更新而主选区不变。
4. 阅读上节源码，回答：这两次观察分别对应 `write_to_primary` 与 `write_to_clipboard` 中的哪条路径？（提示：鼠标选中由编辑器的 selection 变更逻辑触发主选区写入，Ctrl+C 触发普通剪贴板写入。）

**需要观察的现象**：两条通道完全独立、互不污染。

**预期结果**：能用自己的话解释「为什么 Linux 用户习惯中键粘贴刚选中的文字」在协议层如何成立。命令行为待本地验证（取决于你的会话类型与安装的工具）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Wayland 的 `write_to_primary` 要检查 `mouse_focused_window.is_some() || keyboard_focused_window.is_some()`，而 Windows 的 `write_to_clipboard` 不需要类似检查？

**答案**：Wayland 协议把剪贴板/选区所有权绑定到「拥有焦点的 surface」上，且 `set_selection` 必须携带合法输入 serial（[wayland/client.rs:1160-1167](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/client.rs#L1160-L1167)），没有焦点的窗口无权声明。Windows 的剪贴板是全局系统资源，任何进程任何时刻都可以 `OpenClipboard` 后写入，没有焦点约束。

**练习 2**：X11 的 `read_from_clipboard` 为什么在 `is_owner` 时直接返回缓存，而 `read_from_primary` 没有 `clipboard_item` 式的缓存？

**答案**：见 [x11/client.rs:1775-1790](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/x11/client.rs#L1775-L1790)。缓存副本保留了 GPUI 私有元数据（语法高亮等），跨进程协商读回的数据只有标准格式、没有私有格式，所以本进程持有所有权时走缓存能保住元数据；主选区在 Zed 的使用场景里不携带需要保真的元数据，只写纯文本（[x11/client.rs:1739-1750](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/x11/client.rs#L1739-L1750) 用 `item.text()` 降级），因此没有对应缓存。

### 4.3 异步剪贴板：`read_from_clipboard_async` 与浏览器权限模型

#### 4.3.1 概念说明

桌面上读剪贴板是毫秒级的同步调用，但在浏览器里不行：`navigator.clipboard.read()` 是异步 Promise，且受**权限门控**——必须由用户手势（user activation，比如一次点击）触发，浏览器还会弹「允许此网站读取剪贴板吗」的确认。这意味着：

1. 同步签名 `read_from_clipboard() -> Option<ClipboardItem>` 在 Web 上**不可能**实现真实读取。
2. 契约需要一个异步版本，并且要能表达三种失败：剪贴板不可用（非安全上下文）、用户拒绝、内容格式不支持。

gpui 的解法是：`read_from_clipboard` 在 Web 上返回 `None`（诚实的「读不到」），新增带默认实现的 `read_from_clipboard_async`——默认实现包装同步读，因此桌面平台零成本兼容；只有 Web 覆盖它。

#### 4.3.2 核心流程

Web 上一次异步读取的时序（关键约束：`read()` 必须在用户手势的同步调用栈内发起）：

```text
用户点击「粘贴」按钮
   ▼ （同步阶段，仍在手势调用栈内）
read_from_clipboard_async() 被调用
   ├─ Reflect::get 探测 navigator.clipboard 是否存在（非安全上下文为 undefined）
   │    └─ 不存在 → 立即返回 Task::ready(Err(Unavailable))
   └─ navigator.clipboard().read()      ← 必须在这里同步调用！
        返回 Promise，包进 JsFuture
   ▼ （异步阶段）
foreground_executor.spawn(async move {
    await Promise → items
    遍历每个 ClipboardItem 的每个 MIME 类型：
        text/plain        → 读文本 → ClipboardEntry::String
        可识别的 image/*  → 读字节 → ClipboardEntry::Image
        其他（text/html 等）→ 记 saw_unsupported_type = true
    有条目 → Ok(Some(item))
    无条目但见过不支持类型 → Err(UnsupportedContent)
    完全为空 → Ok(None)
})
```

#### 4.3.3 源码精读

[../gpui/src/platform.rs:313-322](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L313-L322) 是默认实现：`Task::ready(Ok(self.read_from_clipboard()))`——桌面平台继承它就自动获得「异步版本等价于同步版本」的行为，文档注释明确说明了覆盖者是权限门控平台。

[../gpui/src/platform.rs:2311-2327](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L2311-L2327) 定义了 `ClipboardReadError` 三变体（`Unavailable` / `Denied(String)` / `UnsupportedContent`），注释点明这些错误要呈现给用户，所以变体设计对应用户可理解的三种引导方向。

[../gpui_web/src/platform.rs:555-557](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_web/src/platform.rs#L555-L557)：Web 的同步读恒返回 `None`——这就是「Web 上调用方应优先用异步版」的机制性原因。

[../gpui_web/src/platform.rs:559-618](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_web/src/platform.rs#L559-L618) 是覆盖实现，两段注释浓缩了浏览器约束的全部要点：探测先行是为了「在 `undefined` 上调方法会让 wasm-bindgen 直接 abort」；`read()` 必须同步调用是为了保住 user activation。随后按 4.3.2 的流程组装条目，MIME 白名单之外的一律跳过（注释解释：其他 web 应用复制的 `text/html` 和自定义格式，`getType` 抓取既浪费又可能被拒绝）。

[../gpui_web/src/platform.rs:620-628](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_web/src/platform.rs#L620-L628)：写入侧 `write_to_clipboard` 用 `write_text`（只支持文本）并 fire-and-forget——同样因为必须在用户输入事件的同步调用栈内发起，返回的 Promise 被直接丢弃。

#### 4.3.4 代码实践

**实践目标**：掌握「同步发起、异步等待」的正确调用姿势，理解为什么不能在 spawn 的任务里才调 `read_from_clipboard_async()`。

**操作步骤**：

1. 阅读下面的示例代码（示例代码，非项目原有，结构参照 [../gpui/examples/window.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/examples/window.rs)）：

   ```rust
   // 示例代码：在点击回调里发起异步剪贴板读取
   .on_click(cx.listener(|_this: &mut MyView, _ev, _window, cx| {
       // 关键：read_from_clipboard_async 必须在回调内同步调用。
       // Web 实现要在 user activation 的同步调用栈里执行
       // navigator.clipboard().read()，推迟到 spawn 内部就晚了。
       let task = cx.read_from_clipboard_async();
       cx.spawn(async move |_| {
           match task.await {
               Ok(Some(item)) => println!("异步读回: {:?}", item.text()),
               Ok(None) => println!("剪贴板为空"),
               Err(err) => println!("读取失败: {err}"),
           }
       })
       .detach();
   }))
   ```

2. 把它抄进你在 u1-l2 建立的窗口小程序里（或本讲综合实践的工程），编译运行。
3. 对比实验：再写一个版本，把 `cx.read_from_clipboard_async()` 挪进 `cx.spawn` 的闭包内部第一行。在桌面平台两者行为相同（默认实现只是包装同步读）；若你能在浏览器里跑（参照 u7-l3 的 trunk 流程），第二个版本预期触发权限失败——因为 user activation 已过期。

**需要观察的现象**：桌面平台上 `Ok(Some(...))` 与同步读结果一致；`Err` 分支在桌面永远走不到（默认实现不产生错误）。

**预期结果**：能解释「发起」与「等待」为什么必须分离。浏览器上的对比实验待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`read_from_clipboard_async` 为什么设计成带默认实现的覆盖点，而不是和 `read_from_clipboard` 一样做成必需方法？

**答案**：四个桌面/移动平台（macOS、Windows、Linux 三后端）的剪贴板读取天然是同步的，如果做成必需方法，每个实现都要写一遍 `Task::ready(Ok(self.read_from_clipboard()))` 的样板。默认实现（[platform.rs:320-322](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L320-L322)）把这行公共代码上收到契约层，只有 Web 需要覆盖——这正是 u2-l1 总结的「通用回退型默认实现」。

**练习 2**：为什么 `ClipboardReadError` 要区分 `Denied` 和 `UnsupportedContent`，而不是统一一个 `Failed(String)`？

**答案**：注释（[platform.rs:2311-2315](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L2311-L2315)）写明这些错误要「呈现给用户并区分引导方向」：`Denied` 意味着可以提示用户去浏览器设置里授权；`UnsupportedContent` 意味着内容确实在剪贴板里但本应用读不了（应提示换内容）；`Unavailable` 则表示这个环境根本没有该 API。三种情况对应的 UI 文案与补救动作都不同。

### 4.4 文件对话框与路径操作：`prompt_for_paths`、`reveal_path` 与 `open_url`

#### 4.4.1 概念说明

这一组方法回答「和操作系统 shell 打交道」的三件事：

- **选文件/文件夹**：`prompt_for_paths`（打开已有文件）与 `prompt_for_new_path`（保存新文件）。返回 `oneshot::Receiver`——对话框由桌面环境弹出，结果异步回传。
- **在文件管理器中显示**：`reveal_path`（定位高亮）与 `open_with_system`（用系统默认程序打开）。
- **打开 URL 与注册 scheme**：`open_url` 让系统浏览器打开链接；`register_url_scheme` 把 `zed://` 这类自定义协议关联到本应用（用于「在浏览器完成 OAuth 后拉起编辑器」）。

各平台的实现选择差异很大：macOS 用原生 `NSOpenPanel`；Linux 不自己画对话框，而是请求 xdg-desktop-portal（通过 ashpd）；Windows 用 Common File Dialog（`FOS_PICKFOLDERS` 标志）。**能力差异**也要向调用方暴露——`can_select_mixed_files_and_dirs` 就是为此存在的布尔探测方法。

#### 4.4.2 核心流程

Linux 上 `prompt_for_paths` 的完整链路：

```text
应用层: cx.prompt_for_paths(PathPromptOptions { files: true, .. })
   ▼
LinuxPlatform::prompt_for_paths          （platform.rs:401）
   ├─ 无 wayland/x11 feature → 直接 done_tx.send(Ok(None)) 返回
   ├─ 取 window_identifier()              （Wayland: 由 wl_surface 导出，
   │                                        X11: 由 X11 window id 导出）
   ▼ foreground_executor().spawn(...).detach()
ashpd OpenFileRequest::default()
   .identifier(identifier.await)   ← portal 用它把对话框父窗口设对
   .modal(true).title("Open File"或"Open Folder")
   .accept_label(options.prompt)
   .multiple(options.multiple)
   .directory(options.directories)
   .send().await                     ← DBus 调用 FileChooser portal
   ├─ Err(PortalNotFound) → done_tx.send(Err(FILE_PICKER_PORTAL_MISSING))
   ├─ Ok(request) → request.response()
   │    ├─ Ok(response)  → uris() 逐个 Url::parse → to_file_path()
   │    │                   → done_tx.send(Ok(Some(paths)))
   │    └─ Err(Response) → 用户取消 → done_tx.send(Ok(None))
   ▼
应用层 await oneshot::Receiver 得到 Result<Option<Vec<PathBuf>>>
```

`open_url` 在 Linux 上还有一层 Wayland 特有的细节：直接启动浏览器会抢走焦点，违反 Wayland 的安全模型，所以优先走 **xdg-activation** 协议——先向 compositor 申请一个 activation token，附带引发这次打开的输入 serial，把 token 通过环境变量 `XDG_ACTIVATION_TOKEN` 传给子进程，compositor 据此判断「这是用户主动触发的启动」并允许焦点转移。

#### 4.4.3 源码精读

**契约与选项模型**：

[../gpui/src/platform.rs:190-201](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L190-L201) 定义了整组方法。注意返回类型分两派：`prompt_for_*` 返回 `oneshot::Receiver`，`register_url_scheme` 返回 `Task`，其余同步。

[../gpui/src/platform.rs:2137-2148](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L2137-L2148) 是 `PathPromptOptions` 四字段：`files` / `directories` / `multiple` / `prompt`（确认按钮文案）。

**Linux 的 portal 实现**（4.4.2 流程图的出处）：

[../gpui_linux/src/linux/platform.rs:401-459](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L401-L459) 值得逐行读：`#[cfg]` 分两版——没编 wayland/x11 时立刻回 `Ok(None)`（headless 场景）；有则先取 `window_identifier`（[../gpui_linux/src/linux/wayland/client.rs:1227-1234](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/client.rs#L1227-L1234) 在 Wayland 后端由 `ashpd::WindowIdentifier::from_wayland(&surface)` 导出）。`PortalNotFound` 被翻译成友好错误 [FILE_PICKER_PORTAL_MISSING](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L47-L49)——这正是 u5-l5 要展开的主题。保存对话框 `prompt_for_new_path`（[../gpui_linux/src/linux/platform.rs:461-521](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L461-L521)）用 `SaveFileRequest`，结构对称。

[../gpui_linux/src/linux/platform.rs:524-527](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L524-L527)：`can_select_mixed_files_and_dirs` 返回 false，注释点明 portal 的 FileChooser 接口只有「选文件」和「选目录」两种模式。对照：macOS 返回 true（[../gpui_macos/src/platform.rs:899-901](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L899-L901)，NSOpenPanel 可同时勾选两者），Windows 返回 false（[../gpui_windows/src/platform.rs:637-639](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L637-L639)，`FOS_PICKFOLDERS` 是二值开关）。调用方（如项目面板）必须先探测再决定是否把「文件+目录混合」的 UI 选项展示给用户。

**关于 xdg_desktop_portal.rs 的澄清**：大纲里列出的 [../gpui_linux/src/linux/xdg_desktop_portal.rs](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/xdg_desktop_portal.rs) 容易让人以为文件选择器在这——实际它封装的是 ashpd 的 **settings** portal：[XDPEventSource::new](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/xdg_desktop_portal.rs#L25-L31) 后台订阅颜色方案（外观）、光标主题、光标大小、按钮布局的变化，经 calloop 通道送回主循环。文件选择器代码在 platform.rs 里直接调 `ashpd::desktop::file_chooser`。两者共享的是「通过 ashpd 走 portal」这一机制，u5-l5 会再回到这个文件。

**open_url 家族**：

[../gpui_linux/src/linux/platform.rs:392-399](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L392-L399)：外壳把 `open_url` 转成后端的 `open_uri`。[../gpui_linux/src/linux/wayland/client.rs:1095-1111](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/client.rs#L1095-L1111) 是 Wayland 版：有 xdg-activation 全局对象时申请 token 并记录 `PendingActivation::Uri`，compositor 回发 token 后才真正启动（token 经环境变量传给子进程，见 [../gpui_linux/src/linux/platform.rs:755-779](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L755-L779) 的 `open_uri_internal`）；没有 activation 支持则退回普通启动。[../gpui_linux/src/linux/platform.rs:533-548](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L533-L548) 的 `open_with_system` 则简单地在后台线程跑 `xdg-open`。

[../gpui_macos/src/platform.rs:711-722](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L711-L722)：macOS 的 `open_url` 只需构造 `NSURL` 后发给 `NSWorkspace openURL:`。[../gpui_macos/src/platform.rs:724-771](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L724-L771) 的 `register_url_scheme` 是唯一真正实现 scheme 注册的平台：要求 macOS 12+、应用已打包（有 bundle identifier），调 `setDefaultApplicationAtURL:toOpenURLsWithScheme:`。Linux（[platform.rs:731-733](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L731-L733)）与 Windows（[../gpui_windows/src/platform.rs:931-933](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L931-L933)）都返回 `Task::ready(Err(...))` 明确告知未实现——比静默假装成功诚实。

**macOS 的原生对话框**：

[../gpui_macos/src/platform.rs:777-825](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L777-L825)：`NSOpenPanel` 的 `canChooseDirectories` / `canChooseFiles` / `allowsMultipleSelection` 三个开关与 `PathPromptOptions` 三字段一一对应，完成回调（Objective-C block）里把 `NSURL` 数组转回 `PathBuf` 并经 oneshot 通道送回。对比 Linux 版，可以看到「同一契约、两种哲学」：macOS 进程内弹出原生面板，Linux 跨进程请求桌面服务。

#### 4.4.4 代码实践

**实践目标**：跑通一次 `prompt_for_paths`，观察 `PathPromptOptions` 各字段在对话框上的效果。

**操作步骤**：

1. 在你的窗口小程序里加一个按钮，点击后发起文件选择（示例代码，非项目原有）：

   ```rust
   // 示例代码：button 的 on_click 回调中
   .on_click(|_, window, cx| {
       use gpui::PathPromptOptions;
       let receiver = window.prompt_for_paths(
           PathPromptOptions {
               files: true,
               directories: false,
               multiple: true,
               prompt: Some("打开这些文件".into()),
           },
           window,
           cx,
       );
       cx.spawn(async move |_| {
           match receiver.await {
               Ok(Ok(Some(paths))) => println!("选中: {paths:?}"),
               Ok(Ok(None)) => println!("用户取消"),
               Ok(Err(e)) => println!("对话框出错: {e}"),
               Err(_) => println!("通道被丢弃"),
           }
       })
       .detach();
   })
   ```

   说明：`Window` 上有 `prompt_for_paths` 的包装方法（它转调平台层并处理焦点冻结），字段名以你本地 rust-analyzer 的补全为准；若 `window.prompt_for_paths` 的签名与上面不符，直接用 `cx.prompt_for_paths(options)`（[../gpui/src/app.rs:1564](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1564)）效果相同。

2. 运行并点击，依次改动验证：`directories: true`（标题应变为「Open Folder」）、`multiple: false`（只能选一个）、`prompt` 文案出现在确认按钮上。
3. 在对话框里点「取消」，确认走 `Ok(Ok(None))` 分支而不是错误。
4. Linux 上额外做一次：把 portal 实现暂时不可用（如在没有桌面环境的服务器会话上跑，或设置 `ZED_HEADLESS=1` 观察无 wayland/x11 时的立即返回），确认错误文案就是 `FILE_PICKER_PORTAL_MISSING`。

**需要观察的现象**：选项字段与对话框行为一一对应；取消是正常返回值。

**预期结果**：记录「字段 → 对话框效果 → 对应平台源码行」的对照表。步骤 4 的 portal 缺失场景待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`prompt_for_paths` 为什么返回 `oneshot::Receiver` 而不是 `Task<Result<...>>`？`register_url_scheme` 却返回 `Task`，两者取舍在哪？

**答案**：文件对话框的完成时机完全由用户操作决定，且实现里结果通过 `done_tx.send(...)` 一次性回传（[linux/platform.rs:405](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L405)、[macos/platform.rs:781](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L781) 都是先建通道再返回接收端）——oneshot 恰好表达「一个值、一次送达」。`register_url_scheme` 的执行体是一个 future（macOS 版 await 内部通道、其他平台 `Task::ready`），用 `Task` 更贴合 GPUI 的异步句柄生态，也天然支持 detach。本质上两者都能表达异步，这是「按语义选外壳」的风格取舍，不是硬性约束。

**练习 2**：为什么 Linux 的 `open_uri` 在有 `globals.activation` 时要多走一段 token 申请，直接 `xdg-open` 不行吗？

**答案**：Wayland 的安全模型不允许后台应用随意抢焦点。没有 activation token 时启动的新窗口可能被 compositor 拒绝置前（或静默不聚焦）。申请 token 时携带引发操作的输入 serial（[wayland/client.rs:1102-1105](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/wayland/client.rs#L1102-L1105)）等于向 compositor 出示「用户刚在我这里点击过」的证据，子进程再通过 `XDG_ACTIVATION_TOKEN` 环境变量转交这份证据。直接 `xdg-open` 是代码里的**降级路径**，仅在 compositor 不支持 xdg-activation 时使用。

### 4.5 凭据三件套：Keychain、Credential Manager 与 Secret Service

#### 4.5.1 概念说明

`write_credentials` / `read_credentials` / `delete_credentials` 以 `url`（服务器标识）为主键存储「用户名 + 密码字节」。Zed 用它保存 GitHub/Sign-in 凭据。三个方法都返回 `Task`，因为三大桌面系统的存储 API 都是潜在阻塞调用，实现方统一把它们扔到执行器上跑——但注意一个有意思的差异：macOS/Linux 用 `background_executor`，Windows 用 `foreground_executor`。

契约的语义约定（从实现归纳）：

- `read_credentials` 查无凭据返回 `Ok(None)`，不算错误；
- 密码是 `Vec<u8>]` 字节而非字符串——允许存非 UTF-8 的 token；
- 三平台用**不同**的存储键策略（见下），但对调用方呈现统一接口。

#### 4.5.2 核心流程

macOS 写入凭据的「先更新、后创建」策略：

```text
write_credentials(url, username, password)
   ▼ background_executor.spawn
构造查询字典: { kSecClass: kSecClassInternetPassword,
                kSecAttrServer: url }
构造更新字典: 查询键 + { kSecAttrAccount: username,
                         kSecValueData: password }
   ▼
SecItemUpdate(查询, 更新)
   ├─ errSecSuccess → 完成（verb = "updating"）
   └─ errSecItemNotFound
        ▼
      SecItemAdd(更新字典)             （verb = "creating"）
      └─ 非 errSecSuccess → 报错 "{verb} password failed: {status}"
```

Linux 侧的键策略不同：Secret Service 的条目有 label + attributes 两层，oo7 按 attributes 搜索。GPUI 给所有条目打上固定 label `zed-github-account`（[KEYRING_LABEL](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L45)），attributes 里存 `url` 和 `username`——所以 `read_credentials` 搜到条目后还要校验 label，避免误读其他应用写入的同属性条目。

#### 4.5.3 源码精读

**契约**：

[../gpui/src/platform.rs:334-336](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L334-L336) 三行定义三个必需方法。应用层转发在 [../gpui/src/app.rs:1462-1472](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/app.rs#L1462-L1472)。

**macOS（Keychain，Internet Password 类条目）**：

[../gpui_macos/src/platform.rs:1143-1182](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L1143-L1182) 是 4.5.2 流程图的出处，`verb` 变量让错误消息区分「更新失败」还是「创建失败」。读取 [../gpui_macos/src/platform.rs:1184-1227](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L1184-L1227) 用 `SecItemCopyMatching`，`errSecItemNotFound` 与 `errSecUserCanceled` 都归一化为 `Ok(None)`（后者是用户拒绝了钥匙串解锁弹窗）。删除 [../gpui_macos/src/platform.rs:1229-1245](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_macos/src/platform.rs#L1229-L1245) 用 `SecItemDelete`。

**Windows（Credential Manager，generic 凭据）**：

[../gpui_windows/src/platform.rs:832-870](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L832-L870)：写入前先检查 blob 大小不超过 `CRED_MAX_CREDENTIAL_BLOB_SIZE`，注释解释了原因——超限时 `CredWriteW` 会报一个难懂的 RPC 错误 `0x800706F7`，所以提前用清晰消息失败。凭据用 `CRED_TYPE_GENERIC` 类型、`CRED_PERSIST_LOCAL_MACHINE` 持久化，目标名经 `windows_credentials_target_name(url)` 规整。读取 [../gpui_windows/src/platform.rs:872-912](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L872-L912) 把 `ERROR_NOT_FOUND` 显式翻译成 `Ok(None)`，注释点名「对齐 macOS 与 Linux 行为」——跨平台语义对齐要靠实现方自觉加这类分支。删除在 [../gpui_windows/src/platform.rs:914-929](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_windows/src/platform.rs#L914-L929)。注意这一组用的是 `foreground_executor`——Win32 凭据 API 的线程亲和性要求与 macOS 的后台跑法不同，是四平台里独有的选择。

**Linux（Secret Service，oo7 crate）**：

[../gpui_linux/src/linux/platform.rs:657-674](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L657-L674)：`oo7::Keyring::new().await?` 连接 Secret Service（GNOME Keyring 或 KWallet，由 oo7 自动协商），`unlock()` 触发必要时的一次系统解锁，然后 `create_item(KEYRING_LABEL, attributes, secret, true)` 写入（最后一个参数立即写盘）。读取 [../gpui_linux/src/linux/platform.rs:676-702](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L676-L702) 按 `url` 属性搜索、按 label 过滤；其中的注释值得注意——oo7 的 secret 本带 zeroize 防残留能力，但 GPUI 凭据 API 以 `Vec<u8>` 返回，防残留特性在这个边界失效，被如实记录为「当前 API 的局限」。删除 [../gpui_linux/src/linux/platform.rs:704-721](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L704-L721) 同样搜索后逐条删除。

**Web（无存储）**：

[../gpui_web/src/platform.rs:630-644](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_web/src/platform.rs#L630-L644)：写入/删除直接报错「credential storage is not available on the web」，读取返回 `Ok(None)`（浏览器环境用其他机制如 localStorage/OAuth 流程管理凭据，不属于平台层职责）。

#### 4.5.4 代码实践

**实践目标**：完成一次凭据写入-读取-删除闭环，并在系统工具里亲眼看到这条记录。

**操作步骤**：

1. 在你的小程序里加一个按钮，串起三步（示例代码，非项目原有）：

   ```rust
   // 示例代码：on_click 回调中依次执行三件套
   cx.write_credentials("example.gpui.test", "gpui-learner", b"secret-bytes")
       .detach();
   let read = cx.read_credentials("example.gpui.test");
   cx.spawn(async move |_| {
       match read.await {
           Ok(Some((user, pass))) => {
               println!("读到凭据: user={user}, password={:?}", String::from_utf8_lossy(&pass));
           }
           other => println!("读取结果: {other:?}"),
       }
   })
   .detach();
   ```

2. macOS 上打开「钥匙串访问」搜 `example.gpui.test`，应能看到一条 Internet Password 条目；Windows 上打开「凭据管理器 → Windows 凭据 → 普通」查找目标名；Linux 上可用 `secret-tool search url example.gpui.test`（需安装 libsecret 工具）观察。
3. 加一行 `cx.delete_credentials("example.gpui.test").detach();`，重跑后确认系统工具里查不到了。

**需要观察的现象**：读回的密码与写入字节一致；系统工具里的记录可被用户在 GUI 里查看/删除——平台层存储没有做额外加密伪装，安全性依赖系统服务本身。

**预期结果**：闭环成功，且能说出自家操作系统上这条数据落在哪个服务里。系统工具的具体操作待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：三个平台的 `read_credentials` 都要处理「不存在」的情况，各自的信号是什么？最终归一化成什么？

**答案**：macOS 是 `SecItemCopyMatching` 返回 `errSecItemNotFound`（或 `errSecUserCanceled`）；Windows 是 `CredReadW` 失败且错误码 `ERROR_NOT_FOUND`；Linux 是 `search_items` 返回空列表或没有 label 匹配的条目。三者都归一化为 `Ok(None)`——这是契约层没有写出来、靠实现约定维持的语义（Windows 实现的注释明确承认了这一点）。

**练习 2**：为什么 Linux 实现给每个条目都打 `KEYRING_LABEL` 并在读取时校验它，而 macOS 实现不需要类似的 label？

**答案**：macOS 的查询字典以 `kSecClassInternetPassword + kSecAttrServer` 为主键，类型系统已经把条目限定为「某服务器的互联网密码」，天然不会误中其他应用的条目。Secret Service 的 attributes 是自由键值对，任何应用都能写 `url=example.com` 属性，只按属性搜索可能误读；固定 label `zed-github-account`（[platform.rs:45](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L45)）作为第二重命名空间过滤（[read_credentials 的过滤逻辑](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui_linux/src/linux/platform.rs#L684-L699)）。

## 5. 综合实践

**任务**：做一个「剪贴板体检」小工具，把本讲全部知识点串起来。

新建独立 crate（或在 u1-l2 的工程上改造），`Cargo.toml` 依赖 `gpui` 与 `gpui_platform`（用 zed 仓库的 path 依赖，feature 按你的平台参照 u1-l3 配置）。程序打开一个窗口，从上到下四个按钮加一块报告区（完整工程骨架，示例代码）：

```rust
// 示例代码：src/main.rs（骨架，各检查逻辑见步骤 1-4）
use gpui::{App, ClipboardItem, WindowOptions, div, prelude::*};
use gpui_platform::application;

struct ClipboardDoctor {
    report: String,
}

// 仿照 examples/window.rs 的 button 工具函数：标签 + 回调 → 元素
fn button(label: &str, on_click: impl Fn(&mut Window, &mut App) + 'static) -> impl IntoElement {
    div()
        .id(label.to_string())
        .px_2()
        .border_1()
        .cursor_pointer()
        .child(label.to_string())
        .on_click(move |_, window, cx| on_click(window, cx))
}

impl Render for ClipboardDoctor {
    fn render(&mut self, _window: &mut Window, cx: &mut Context<Self>) -> impl IntoElement {
        div()
            .id("root")
            .p_4()
            .flex()
            .flex_col()
            .gap_2()
            // 用 cx.listener 把回调绑定到 &mut ClipboardDoctor，
            // 各 handler 的实现见步骤 1-4
            .child(button("① 写入并同步读回", cx.listener(Self::check_roundtrip)))
            .child(button("② 异步读回", cx.listener(Self::check_async)))
            .child(button("③ 主选区", cx.listener(Self::check_primary)))
            .child(button("④ 文件对话框", cx.listener(Self::check_file_dialog)))
            .child(div().child(self.report.clone()))
    }
}

fn main() {
    application().run(|cx: &mut App| {
        cx.open_window(WindowOptions::default(), |_, cx| {
            cx.new(|_| ClipboardDoctor {
                report: "点击按钮开始体检".into(),
            })
        })
        .unwrap();
        cx.activate(true);
    });
}
```

按步骤补齐（每一项对应本讲一个模块；四个 `check_*` 方法签名统一为 `fn(&mut self, _ev: &ClickEvent, _window: &mut Window, cx: &mut Context<Self>)`，直接传给 `cx.listener`）：

1. **① 写入并同步读回**（4.1）：`cx.write_to_clipboard(ClipboardItem::new_string("gpui-doctor".into()))` 后立刻 `cx.read_from_clipboard().and_then(|i| i.text())`，比对是否相等，把结果写进 `self.report` 并 `cx.notify()`。
2. **② 异步读回**（4.3）：在回调内**同步**调用 `cx.read_from_clipboard_async()` 拿到 task，再 `cx.spawn` 里 await，记录它返回 `Ok(Some)/Ok(None)/Err` 哪个分支。
3. **③ 主选区**（4.2）：整段用 `#[cfg(any(target_os = "linux", target_os = "freebsd"))]` 包裹，`cx.write_to_primary(...)` 后 `cx.read_from_primary()` 读回；非 Linux 平台按钮改显示「本平台无主选区」（用 `.when(cfg!(...), ...)` 控制文案）。
4. **④ 文件对话框**（4.4）：`cx.prompt_for_paths(PathPromptOptions { files: true, directories: true, multiple: true, prompt: Some("体检选文件".into()) })`，把结果（路径列表 / 用户取消 / 错误）写进报告。注意在你的平台上 `cx.can_select_mixed_files_and_dirs()` 返回什么，mixed 选项是否应该展示。
5. **收尾笔记**（对应任务要求）：对照 4.1.2 与 4.4.2 的流程图，把每一步在你操作系统上的真实调用路径（App 转发行号 → 平台实现文件:行号）写成 Markdown 笔记，附在工程里。
6. **可选加餐**（4.5）：再加一个按钮走一遍凭据三件套闭环。

**验收标准**：①②在所有桌面平台通过；③在 Linux 上通过且与 4.2.4 的命令行观察互相印证；④能区分「选中」与「取消」两条返回路径；笔记能指到具体源码行。整个工程的编译与运行待本地验证。

## 6. 本讲小结

- 剪贴板契约只有两个必需方法（`read_from_clipboard` / `write_to_clipboard`），复杂性在 `ClipboardItem` 的多格式条目模型；GPUI 还在 macOS/Windows 剪贴板里私藏「文本哈希 + 元数据」两个自定义格式，用于进程内粘贴时恢复语法高亮等附加信息。
- 平台专属剪贴板用 `#[cfg]` 门控进 trait：主选区只存在于 Linux/FreeBSD，查找板只存在于 macOS；Linux 侧再经 `LinuxPlatform` 外壳转发给 Wayland / X11 / headless 三个 `LinuxClient` 后端，Wayland 版要处理焦点与 serial 约束，X11 版有 `is_owner` 读缓存优化。
- `read_from_clipboard_async` 是带默认实现的覆盖点：桌面平台默认等价于同步读，仅 Web 覆盖——浏览器的 `navigator.clipboard.read()` 必须在用户手势的同步调用栈内发起，且失败要用 `ClipboardReadError` 三变体区分引导方向。
- 文件对话框三个平台三种哲学：macOS 原生 NSOpenPanel、Windows Common Dialog、Linux 跨进程请求 xdg-desktop-portal（ashpd）；能力差异通过 `can_select_mixed_files_and_dirs` 这类布尔探测暴露给调用方，portal 缺失被翻译成用户可读的 `FILE_PICKER_PORTAL_MISSING`。
- `open_url` 在 Wayland 上要走 xdg-activation token 才能合规地转移焦点；`register_url_scheme` 只有 macOS 真正实现，Linux/Windows 返回明确的「未实现」错误。
- 凭据三件套分别落到 Keychain（`SecItem*`，先更新后创建）、Credential Manager（`Cred*W`，blob 有大小上限）、Secret Service（oo7，label + attributes 双层键）；「查无凭据返回 `Ok(None)`」的语义对齐靠各实现自觉处理各自的「不存在」信号。

## 7. 下一步学习建议

本讲之后，u2 单元的 Platform trait 契约之旅就完整了。建议两条继续路线：

1. **横向深入平台实现**：u5-l1 将拆开 `LinuxPlatform` 与 `LinuxClient` 的完整后端结构，u5-l5 会专门展开 xdg-desktop-portal 与 ashpd（本讲 4.4 的延伸）；u6-l3 覆盖 macOS/Windows 的系统通知与菜单——同属「系统集成」组的其余成员。
2. **纵向换主题**：如果你对「对话框弹出期间窗口焦点如何冻结」这类窗口层问题感兴趣，先读 u3-l2（PlatformWindow trait）；想理解 `Task` / `foreground_executor` / `background_executor` 的底层机制，进入 u4-l1。

源码阅读练习：把 [../gpui/src/platform.rs:186-201](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L186-L201) 与 [../gpui/src/platform.rs:310-336](https://github.com/zed-industries/zed/blob/6e0a0835755ea57c1db4e0057f1a30ddba554706/crates/gpui/src/platform.rs#L310-L336) 两个区段的每个方法，在四个平台 crate 里各找到一个 `fn` 实现位置，做成一张「契约方法 × 平台实现」索引表——这张表会是后续各讲反复用到的查阅工具。
