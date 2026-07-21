# 打包、安装与 Sparkle 更新

## 1. 本讲目标

本讲解决一个工程问题：Squirrel 的 Swift 源码编译成 `Squirrel.app` 之后，**怎样把它变成普通 macOS 用户双击就能装的 `.pkg` 安装包，安装时怎样自动完成「注册输入源 + 预编译方案 + 启用 + 选中」，以及发布后怎样通过 Sparkle 实现自动更新**。

学完本讲你应当能够：

- 说清楚 `pkgbuild` 和 `productbuild` 这两个 Apple 官方工具的分工，以及 Squirrel 用它们把 `.app` 包成 `.pkg` 的完整流程。
- 理解代码签名（codesign）、公证（notarytool）和装订（stapler）这条「让 Gatekeeper 放行」的发布流水线。
- 逐行解释 `scripts/postinstall` 在安装时执行的 `killall → --register-input-source → --build → --enable-input-source → --select-input-source` 五步，以及它们分别以什么用户身份运行。
- 说明 `Info.plist` 中 `SUFeedURL` 与 `SUPublicEDKey` 在 Sparkle 自动更新里分别扮演什么角色，并理解 appcast + EdDSA 签名的更新校验机制。

## 2. 前置知识

在进入本讲前，建议你已经建立以下认知（这些在前序讲义中讲过）：

- **macOS 应用的分发形式**：一个 `.app` 本质是一个目录（bundle），把可执行文件、资源、动态库都按固定结构打包在内；而 `.pkg`（flat package）是 macOS 系统安装器（Installer）能识别的「安装包」格式，双击后弹出图形安装向导。
- **代码签名与 Gatekeeper**：macOS 默认会阻止运行「来自未识别开发者」的应用。开发者需要用 Apple 签发的 **Developer ID** 证书对应用签名，再交给 Apple **公证（notarize）**，通过后用户才能正常打开。
- **TIS 输入源生命周期**：这是 [u5-l2 输入源注册（TIS）](u5-l2-input-source-registration.md) 的核心——一个输入法要被系统承认，必须经历 `register（登记）→ enable（启用）→ select（选中）` 三步，分别对应 Squirrel 可执行文件的 `--register-input-source` / `--enable-input-source` / `--select-input-source` 命令行参数。
- **Squirrel 是单二进制双身份**：这是 [u1-l4 程序入口与启动流程](u1-l4-entry-and-startup.md) 讲过的关键设计——同一个 `Squirrel` 可执行文件，带命令行参数运行时是「一次性工具」（执行完就退出），不带参数运行时才是「常驻输入法」。`postinstall` 正是利用这一点，用同一个二进制完成安装期的注册工作。
- **Sparkle**：第三方 macOS 应用常用的自动更新框架（Squirrel 以 git submodule 形式引入，见 [u1-l1](u1-l1-project-overview.md)）。它周期性地从一个 URL（appcast）拉取版本信息，发现新版本就下载并校验签名后安装。

> 本讲不涉及输入法运行时的按键处理，只讲「分发、安装、更新」这条独立的工程链路。

## 3. 本讲源码地图

本讲涉及的文件分工如下：

| 文件 | 作用 |
| --- | --- |
| [Makefile](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile) | 总调度：定义 `package` / `archive` 等目标，把签名、打包、公证、装订、生成 appcast 串成一条流水线。 |
| [package/make_package](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/make_package) | 调 `pkgbuild` 造组件包、调 `productbuild` 包装成最终 `Squirrel.pkg`。 |
| [package/Distribution](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/Distribution) | `productbuild` 的「分发脚本」模板，声明标题、架构、安装后要求注销等。 |
| [package/Squirrel-component.plist](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/Squirrel-component.plist) | `pkgbuild` 的组件属性表，控制 bundle 的覆盖、重定位行为。 |
| [package/sign_app](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/sign_app) | 用 `codesign` 对 `.app` 做 Developer ID 签名，再用 `spctl` 自检。 |
| [scripts/postinstall](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/scripts/postinstall) | 安装器在拷贝完文件后执行的脚本，跑注册/预编译/启用/选中五步。 |
| [sources/Main.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift) | 解析 `--register-input-source` / `--build` / `--enable-input-source` / `--select-input-source` 等参数。 |
| [resources/Info.plist](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist) | 声明 `SUFeedURL` / `SUPublicEDKey` 等 Sparkle 配置。 |
| [package/make_archive](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/make_archive) | 用 `sign_update` 生成 EdDSA 签名，产出 appcast.xml 更新源。 |
| [sources/SquirrelApplicationDelegate.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift) | 运行时持有 Sparkle 的 `updateController`，提供「检查更新」入口。 |

## 4. 核心概念与源码讲解

### 4.1 .pkg 安装包的构建：pkgbuild + productbuild

#### 4.1.1 概念说明

把一个 `.app` 交给用户，最朴素的方式是塞进 zip 让用户拖到 `/Applications`。但输入法不行——它必须装到 `/Library/Input Methods/`（一个需要管理员权限的系统目录），还要在安装时跑一段脚本来注册输入源。这种「需要管理员权限 + 需要执行脚本 + 需要图形向导」的场景，正是 macOS 系统安装器（Installer）和 `.pkg` 包的用武之地。

Apple 提供两个互补的命令行工具：

- **`pkgbuild`**：把一个目录（通常是编译产物 `Release/`）打包成一个**组件包（component package）**，它只关心「把这些文件原样铺到某个安装位置，附带一些 bundle 覆盖规则和安装脚本」。
- **`productbuild`**：把一个或多个组件包，再加上**分发脚本（Distribution）**、本地化资源、欢迎/结论页面，包装成一个**产品归档（product archive）**——也就是用户双击看到的那个带标题、带向导页的 `.pkg`。

一句话区分：`pkgbuild` 造「砖」（组件包），`productbuild` 把砖砌成「房子」（带 UI 的最终安装包）。Squirrel 的 `make_package` 脚本就是这两步的顺序组合。

#### 4.1.2 核心流程

`make package` 的整体链路（参见 Makefile 的 `package` 与 `$(PACKAGE)` 目标）：

```
make package
   │
   ├─ release         (依赖：先编出 Squirrel.app，见 u1-l3)
   │
   └─ $(PACKAGE)      (造 Squirrel.pkg)
        │
        ├─ [若设了 DEV_ID] package/sign_app     → codesign 签名 .app
        ├─ package/make_package                 → pkgbuild + productbuild
        └─ [若设了 DEV_ID] productsign + notarytool + stapler  → 签名公证装订 .pkg
```

`package/make_package` 内部两步：

```
pkgbuild  --root .../Release  --component-plist Squirrel-component.plist
          --identifier im.rime.inputmethod.Squirrel  --version <版本号>
          --install-location '/Library/Input Methods'  --scripts scripts/
          → Squirrel-component.pkg

productbuild  --distribution Distribution-versioned.xml
              --package-path .  --resources .
              → Squirrel.pkg
```

`productbuild` 完成后，组件包 `Squirrel-component.pkg` 是中间产物，会被删掉，只留最终的 `Squirrel.pkg`。

#### 4.1.3 源码精读

先看 Makefile 里造包目标的总调度。注意 `$(PACKAGE)` 这条规则用 `ifdef DEV_ID` 把「签名/公证」包夹起来——也就是说**不设 `DEV_ID` 也能打出包，只是这个包没有签名、没法过 Gatekeeper**，适合本地自测。

[Makefile:L138-L156](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L138-L156) 是 `package` 与 `archive` 目标，其中造包的核心逻辑在第 140–151 行：

- 第 141–143 行：设了 `DEV_ID` 才调用 `sign_app` 给 `.app` 签名。
- 第 144 行：无论是否签名，都调用 `make_package` 造 `.pkg`。
- 第 145–151 行：设了 `DEV_ID` 时，对 `.pkg` 做 `productsign`（包签名）+ `notarytool submit`（公证）+ `stapler staple`（装订），这些在 4.2 节详解。

`PACKAGE = package/Squirrel.pkg`（[Makefile:L25](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L25)）定义了最终产物的路径。

接下来看 `make_package` 脚本本身。

[package/make_package:L12-L20](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/make_package#L12-L20) 是 `pkgbuild` 调用，几个关键参数：

| 参数 | 含义 |
| --- | --- |
| `--root .../Release` | 被打包的根目录，里面应有 `Squirrel.app`。 |
| `--filter '.*\.swiftmodule$'` | 过滤掉 `.swiftmodule` 文件（Swift 模块接口，运行时不需要）。 |
| `--component-plist Squirrel-component.plist` | 组件属性表，控制 bundle 覆盖行为（见下）。 |
| `--identifier im.rime.inputmethod.Squirrel` | 包的 bundle identifier。 |
| `--version "$(get_app_version)"` | 版本号，取自 Xcode 项目的 `CURRENT_PROJECT_VERSION`。 |
| `--install-location '/Library/Input Methods'` | 铺到系统的输入法目录。 |
| `--scripts "${PROJECT_ROOT}/scripts"` | **关键**：把 `scripts/` 目录里的 `postinstall` 打进包里，安装时自动执行。 |

[package/make_package:L22](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/make_package#L22) 用 `sed` 把 `Distribution` 模板里的 `@VERSION@` 占位符替换成实际版本号，生成 `Distribution-versioned.xml`。这一步先于 `productbuild` 完成，因为分发脚本里的标题（如 `Squirrel-1.0.0`）需要带具体版本。

[package/make_package:L24-L28](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/make_package#L24-L28) 是 `productbuild` 调用：`--distribution` 指向刚才生成的版本化分发脚本，`--package-path .` 告诉它去当前目录找组件包 `Squirrel-component.pkg`，`--resources .` 引入本地化资源（欢迎页、结论页的多语言版本，仓库里 `package/zh_CN.lproj/`、`package/zh_TW.lproj/` 即是）。

版本号从哪来？看 `common.sh`：

[package/common.sh:L11-L14](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/common.sh#L11-L14) 的 `get_app_version()` 调 `xcrun agvtool what-version` 读 Xcode 项目里的 `CURRENT_PROJECT_VERSION`，再用 `sed` 提取数字。所以 `.pkg` 的版本号与 App 内部版本号是同一个来源，保持一致。

分发脚本 `Distribution` 决定安装器的行为：

[package/Distribution:L1-L17](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/Distribution#L1-L17) 中几个值得注意的点：

- 第 3 行 `<title>Squirrel-@VERSION@</title>`：安装器窗口标题，`@VERSION@` 由上面的 `sed` 替换。
- 第 4 行 `<conclusion file="conclusion.html" .../>`：安装结束后显示的结论页（[package/conclusion.html](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/conclusion.html)），提示用户「请注销重新登录，并在系统设置的「文字输入」里添加此输入法」。
- 第 6 行 `<options customize="never" hostArchitectures="arm64,x86_64" require-scripts="false" rootVolumeOnly="true"/>`：`hostArchitectures="arm64,x86_64"` 声明本包是**通用二进制**（同时含 Apple Silicon 与 Intel），这正是最近一次提交 `2158538 fix(installer): declare host architectures` 修的事——不声明的话安装器在某些情况下会拒绝在某一架构上安装；`rootVolumeOnly="true"` 要求装到启动盘；`customize="never"` 禁用自定义安装选项。
- 第 16 行 `onConclusion="RequireLogout"`：安装完成后**要求用户注销**，这是因为输入法进程需要重新加载。

组件属性表 `Squirrel-component.plist` 控制 `Squirrel.app` 这个 bundle 在升级时的行为：

[package/Squirrel-component.plist:L4-L26](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/Squirrel-component.plist#L4-L26)：

- `BundleIsRelocatable = false`：禁止安装器把 App「重定位」到 `/Applications` 等其他位置，必须老老实实装到 `--install-location` 指定的 `/Library/Input Methods`。
- `BundleOverwriteAction = upgrade`：升级安装时覆盖旧版本。
- `ChildBundles` 列出 `Squirrel.app/Contents/Frameworks/Sparkle.framework`：明确告诉安装器这个内嵌框架也是本次组件的一部分，升级时一并处理。这一点很重要——Sparkle 框架是嵌在 App 里的动态库，如果不声明为子 bundle，升级时可能被误留旧版。

#### 4.1.4 代码实践

**实践目标**：在不实际编译（避免依赖 Xcode）的前提下，靠阅读源码还原「一个 `.app` 是怎样变成 `.pkg`」的命令序列，并理解每个参数的去留对产物的影响。

**操作步骤**：

1. 打开 [package/make_package](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/make_package)，把 `pkgbuild` 那一段（第 12–20 行）的 7 个参数抄下来。
2. 在 [Makefile:L140-L153](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L140-L153) 里找到「谁调用了 `make_package`」「`make_package` 收到的第一个参数 `$1` 是什么」（提示：`DERIVED_DATA_PATH`，即 `build/`）。
3. 思考三个「如果删掉会怎样」的问题：
   - 删掉 `--scripts "${PROJECT_ROOT}/scripts"`：安装后还会自动注册输入源吗？（不会，4.3 节解释）
   - 删掉 `--filter '.*\.swiftmodule$'`：包会变大还是会变小？（变大，多出无用的 Swift 模块接口文件）
   - 删掉 `--install-location '/Library/Input Methods'`：App 会被铺到哪？（根目录 `/Squirrel.app`，输入法无法被系统识别）

**需要观察的现象**：你应当能画出一张「`Release/` 目录 → pkgbuild → `Squirrel-component.pkg` → productbuild → `Squirrel.pkg`」的数据流图，并标注每一步的输入输出文件名。

**预期结果**：理解 `pkgbuild` 负责「文件 + 脚本 + 安装位置」，`productbuild` 负责「向导 UI + 分发脚本 + 本地化资源」。

> 这一步没有实际运行命令（需要 macOS + Xcode + 已编译产物），属于「源码阅读型实践」。如果你手头有 macOS 环境，可在执行过 `make release` 后，手动跑 `bash package/make_package build` 观察产物。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `make_package` 要先 `pkgbuild` 出 `Squirrel-component.pkg`，再立刻 `productbuild` 把它包进 `Squirrel.pkg`，最后还把 `Squirrel-component.pkg` 删掉（[make_package:L32](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/make_package#L32)）？能不能直接产出 `Squirrel.pkg`？

**答案**：`pkgbuild` 和 `productbuild` 是两个职责不同的工具——前者只懂「文件 + 脚本」，后者才懂「分发脚本 + 向导 UI」。必须先用前者造组件包，后者才能引用它。`Squirrel-component.pkg` 只是中间产物，发布给用户的只有带向导的 `Squirrel.pkg`，所以最后删掉中间产物保持目录整洁。不能跳过，因为没有 productbuild 就没有分发脚本，也就没有安装标题、结论页和架构声明。

**练习 2**：[Distribution:L16](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/Distribution#L16) 的 `onConclusion="RequireLogout"` 与 [conclusion.html](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/conclusion.html) 的文字提示，二者配合要达到什么效果？

**答案**：`onConclusion="RequireLogout"` 让系统安装器在结束时弹出「立即注销/稍后」按钮，而 `conclusion.html` 用人话解释了为什么要注销——输入法是常驻进程（见 u1-l4），旧的 Squirrel 进程在安装时已被 `killall`（见 4.3），需要注销重登（或重启）才能让系统用新装好的输入法；同时提醒用户去「系统设置 → 文字输入」里把 Squirrel 添加进来。

---

### 4.2 代码签名与公证：codesign + notarytool

#### 4.2.1 概念说明

即使你把 `.pkg` 造得完美，用户双击时也多半会被 Gatekeeper 拦下：「无法打开，因为它来自身份不明的开发者」。要让用户顺畅安装，发布版必须走完三件事：

1. **代码签名（codesign）**：用开发者从 Apple 申请的 **Developer ID** 证书，对 App 及其内嵌的动态库、框架做数字签名。签名后，macOS 能验证「这个 App 确实出自证书持有者，且未被篡改」。
2. **公证（notarization）**：把打包好的 `.pkg`（或 `.zip`）上传给 Apple 的公证服务，Apple 自动扫描恶意代码，通过后返回一个「公证票据（ticket）」。Gatekeeper 会优先放行带公证票据的软件。
3. **装订（stapling）**：公证票据默认是在线查询的；为了让用户在离线状态下也能安装，用 `stapler` 把票据「装订」（嵌入）到包里。

涉及两类证书，容易混淆：

- **Developer ID Application**：用来签 **App 本体**（`.app` 及其内嵌二进制）。
- **Developer ID Installer**：用来签 **安装包**（`.pkg`）。

Squirrel 的发布流程两者都用：先用 Application 证书签 `.app`，再用 Installer 证书签整个 `.pkg`。

#### 4.2.2 核心流程

```
sign_app                      # 签 .app
   codesign --deep --force --options runtime --timestamp
            --sign "Developer ID Application: <DEV_ID>"
            --entitlements resources/Squirrel.entitlements
   spctl -a -vv               # 自检 Gatekeeper 评估
        ↓
make_package                  # 打 .pkg（见 4.1）
        ↓
productsign                   # 签 .pkg
   --sign "Developer ID Installer: <DEV_ID>"
        ↓
notarytool submit --wait      # 上传公证并等待结果
        ↓
stapler staple                # 装订公证票据
```

整条链路只有在 Makefile 里设了 `DEV_ID` 时才会触发——本地调试构建可以跳过。

#### 4.2.3 源码精读

先看签 App 的脚本 [package/sign_app](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/sign_app)：

[package/sign_app:L9](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/sign_app#L9) 是核心 `codesign` 命令，逐个 flag 解释：

| flag | 作用 |
| --- | --- |
| `--deep` | 递归签名 App 内嵌的 Framework（如 Sparkle.framework）和动态库（如 librime 的 dylib）。 |
| `--force` | 覆盖已有签名（开发期多次重签）。 |
| `--options runtime` | 启用** hardened runtime（硬化运行时）**，这是公证的强制要求，禁用一些危险权限。 |
| `--timestamp` | 向 Apple 时间戳服务器请求带时间戳的签名，保证证书过期后旧签名仍可验证。 |
| `--sign "Developer ID Application: ${DEV_ID}"` | 用 Developer ID Application 证书签。 |
| `--entitlements resources/Squirrel.entitlements` | 声明 App 运行时所需的权限（entitlements），如访问网络、用户通知等。 |

[package/sign_app:L11](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/sign_app#L11) 的 `spctl -a -vv "$appDir"` 是**自检**：`spctl`（Security Assessment Policy）就是 Gatekeeper 背后的评估工具，`-a -vv` 模拟 Gatekeeper 评估并打印详细信息。这一步让发布者在造包阶段就能发现签名问题，而不是等用户安装失败。

签完 App、打完 `.pkg` 之后，回到 Makefile 对 `.pkg` 做签名、公证、装订：

[Makefile:L145-L151](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L145-L151)：

- 第 146 行 `productsign --sign "Developer ID Installer: $(DEV_ID)" ...`：用 **Installer** 证书对 `.pkg` 签名，注意证书名里的关键词是 `Installer`，与签 App 的 `Application` 是两张不同证书。`productsign` 是 productbuild 系列工具里专门签包的。
- 第 147–148 行：签名后用「先存成 `Squirrel-signed.pkg`、再替换原名」的两步走，避免直接覆盖导致的问题。
- 第 149 行 `xcrun notarytool submit package/Squirrel.pkg --keychain-profile "$(DEV_ID)" --wait`：把签好的包提交给 Apple 公证服务。`--keychain-profile` 指向预先存在钥匙串里的 App-specific 密码（API 凭证），`--wait` 让命令**阻塞到公证完成**（公证通常几分钟到十几分钟），这样脚本后续步骤才能确定结果。
- 第 150 行 `xcrun stapler staple package/Squirrel.pkg`：把公证票据装订进包。

> 注意 `notarytool` 与旧的 `altool` 区别：`notarytool` 是 Apple 自 macOS 12 / Xcode 13 起推荐的公证工具，支持 `--wait` 阻塞、`--keychain-profile` 凭证管理，比已弃用的 `altool` 更稳定。

#### 4.2.4 代码实践

**实践目标**：通过阅读源码，把「Developer ID Application」与「Developer ID Installer」这两张证书各自出现在流程的哪一步理清楚，并理解 `--options runtime` 为什么是公证的前置条件。

**操作步骤**：

1. 在 [package/sign_app:L9](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/sign_app#L9) 找到 Application 证书的使用点。
2. 在 [Makefile:L146](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L146) 找到 Installer 证书的使用点。
3. 对照 [resources/Squirrel.entitlements](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Squirrel.entitlements)（可在仓库自行打开），看看 App 声明了哪些运行时权限，思考这些权限与硬化运行时的关系。

**需要观察的现象**：你会看到两张证书**绝不可互换**——签 App 必须用 Application 证书，签 pkg 必须用 Installer 证书；`codesign` 那一步若没有 `--options runtime`，后续 `notarytool` 即便上传成功也会被判失败。

**预期结果**：理解「签名 → 公证 → 装订」是让 `.pkg` 通过 Gatekeeper 的三段流水线，缺一不可。

**待本地验证**：`notarytool submit --wait` 的实际耗时（取决于 Apple 服务排队情况，通常几分钟）。本讲不模拟运行。

#### 4.2.5 小练习与答案

**练习 1**：如果发布者只执行了 `codesign` 签了 App，却没有跑 `notarytool` 和 `stapler`，用户双击 `.pkg` 会发生什么？

**答案**：包能装上（因为 pkg 本身可能也有 productsign 签名），但首次运行 Squirrel.app 时，Gatekeeper 会因为找不到公证票据而拦截，弹出「无法验证开发者」警告，用户必须右键「打开」或去「系统设置 → 隐私与安全性」里手动允许。公证 + 装订正是为了消除这道警告，让更新对普通用户无感。

**练习 2**：[Makefile:L149](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L149) 的 `notarytool submit` 加了 `--wait`。如果去掉 `--wait`，紧接着的 `stapler staple`（第 150 行）会出什么问题？

**答案**：`--wait` 让命令阻塞到 Apple 完成扫描并返回结果。去掉后，`notarytool submit` 会立刻返回（只提交不等待），此时公证可能还没完成，`stapler staple` 就拿不到公证票据，装订会失败。所以 `--wait` 是保证「先有票据、再装订」顺序的关键。

---

### 4.3 postinstall 安装脚本：注册、构建、启用、选中

#### 4.3.1 概念说明

`postinstall` 是 macOS 安装器的一个约定：在 `.pkg` 把所有文件铺到目标位置**之后**、向用户报告安装成功**之前**，安装器会以 **root** 身份执行包内 `scripts/` 目录下的 `postinstall` 脚本（还记得 4.1 里 `pkgbuild --scripts "${PROJECT_ROOT}/scripts"` 把它打进了包吗？）。

Squirrel 的 `postinstall` 干的是「让刚装好的输入法立刻可用」的全部收尾工作：

- 先杀掉可能在跑的旧 Squirrel 进程，避免文件占用。
- 调 `--register-input-source` 把 Squirrel 登记进系统的 TIS 输入源注册表（详见 [u5-l2](u5-l2-input-source-registration.md)）。
- 调 `--build` 预编译 SharedSupport 里的输入方案（生成 `.bin` 缓存，加快首次输入）。
- 调 `--enable-input-source` 把它加入输入法列表。
- 调 `--select-input-source` 把它设为当前输入法。

这里有一个容易踩坑的细节：**不同的命令要以不同的用户身份运行**。TIS 注册是写系统级数据库，以 root（安装器默认身份）即可；但「启用 / 选中」是**用户级偏好**（每个用户的输入法选择是独立的），必须以**当前登录用户**身份执行，否则会注册到 root 的偏好里、对真实用户无效。

#### 4.3.2 核心流程

```
postinstall（以 root 身份被安装器调用）
   │
   ├─ login_user = stat -f%Su /dev/console   # 取得当前登录用户
   │
   ├─ sudo -u login_user killall Squirrel     # 停掉旧进程（以登录用户身份）
   │
   ├─ Squirrel --register-input-source        # 以 root 注册输入源
   │
   ├─ [若未设 RIME_NO_PREBUILD]
   │    cd SharedSupport
   │    Squirrel --build                      # 预编译方案（以 root）
   │
   ├─ sudo -u login_user Squirrel --enable-input-source   # 以登录用户启用
   └─ sudo -u login_user Squirrel --select-input-source  # 以登录用户选中
```

这些命令行参数的真正实现在 `Main.swift`——它依赖「单二进制双身份」设计（见 u1-l4）：带参数运行时，Squirrel 不是输入法，而是一次性工具，干完就 `return` 退出。

#### 4.3.3 源码精读

先看 `postinstall` 全文，它是本模块的主角。

[scripts/postinstall:L1-L22](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/scripts/postinstall#L1-L22) 逐段拆解：

- 第 4 行 `login_user=`/usr/bin/stat -f%Su /dev/console``：`/dev/console` 的属主就是当前登录的 GUI 用户，`stat -f%Su` 取其用户名。这是脚本里获取「真正在使用电脑的人」的标准手法。
- 第 5–8 行：定位刚装好的 App 及其内部可执行文件、SharedSupport 目录。注意 `DSTROOT` 是安装器注入的环境变量（指向安装根，正常是 `/Library/Input Methods`）。
- 第 10 行 `/usr/bin/sudo -u "${login_user}" /usr/bin/killall Squirrel || true`：以**登录用户**身份 `killall` 杀掉旧 Squirrel 进程。为什么要以登录用户身份？因为进程是以该用户启动的。`|| true` 保证「没有进程在跑」时脚本不因 `killall` 报错而中断（`set -e` 在第 2 行已开启，任何命令失败都会让脚本退出）。
- 第 12 行 `"${squirrel_executable}" --register-input-source`：**以 root**（安装器身份）注册输入源。
- 第 14–18 行：若环境变量 `RIME_NO_PREBUILD` 为空（未设置），进入 SharedSupport 目录跑 `--build` 预编译方案。`pushd/popd` 保证目录切换不污染后续命令。`&&` 把 build 与随后的 enable/select 串起来。
- 第 19–20 行：以 **登录用户**身份跑 `--enable-input-source` 和 `--select-input-source`。

> **关于 `RIME_NO_PREBUILD`**：这是 [Makefile:L169](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L169) 的 `install-debug` 目标特意设置的：`DSTROOT="$(DSTROOT)" RIME_NO_PREBUILD=1 bash scripts/postinstall`。开发自测时跳过耗时的预编译以加快迭代，正式 `.pkg` 安装时不设这个变量，会完整预编译。

现在看这些命令在 Swift 端的实现。它们都在 `Main.swift` 的命令行分支里。

[sources/Main.swift:L40-L42](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L40-L42) 处理 `--register-input-source`（别名 `--install`），直接调用 `installer.register()`——这就是 [u5-l2](u5-l2-input-source-registration.md) 讲的 TIS 注册第一步。

[sources/Main.swift:L70-L77](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L70-L77) 处理 `--build`：

- 第 71 行先弹一条用户通知「正在部署」。
- 第 72–73 行用一个独立的 `RimeTraits`（`app_name = "rime.squirrel-builder"`）初始化一个**部署器（deployer）**而非运行时引擎。
- 第 74–76 行 `setup` → `deployer_initialize` → `deploy`：这是 librime 的部署接口，把 YAML 方案编译成二进制 `.bin` 缓存（参见 [u2-l2](u2-l2-global-rime-init.md) 的 `start_maintenance` 概念，但 `--build` 用的是更完整的 `deploy`）。注意它**没有 initialize 一个会话引擎**，只是跑一遍部署。

[sources/Main.swift:L43-L52](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L43-L52) 是 `--enable-input-source`：支持可选的「模式列表」参数（如 `Hans` / `Hant`，参见 u5-l2 的 InputMode 枚举），不传则启用默认模式。

[sources/Main.swift:L63-L69](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L63-L69) 是 `--select-input-source`：可指定要选中的模式，不传则选中默认（Hans）。

每个 case 都 `return true`，于是 [sources/Main.swift:L121-L123](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L121-L123) 的 `if handled { return }` 让程序立刻退出，**绝不进入输入法主循环**——这就是「单二进制双身份」的收口。

#### 4.3.4 代码实践

**实践目标**：跟踪 `postinstall` 的五步执行序列，标注每一步以什么用户身份运行、调用 Squirrel 二进制的哪个参数、对应 `Main.swift` 哪个分支。

**操作步骤**：

1. 打开 [scripts/postinstall](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/scripts/postinstall)，把第 10、12、16、19、20 行五条命令抄成一张表，每行标注「用户身份」「参数」「Main.swift 分支」。
2. 在 [sources/Main.swift:L160-L176](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/Main.swift#L160-L176) 的 `helpDoc` 里核对这四个参数的官方说明（`--build` / `--register-input-source` / `--enable-input-source` / `--select-input-source`）。
3. 思考：为什么第 12 行的 `--register-input-source` 不像第 19、20 行那样加 `sudo -u "${login_user}"`？

**需要观察的现象**：你会清晰地看到「register/build 以 root 跑、enable/select 以登录用户跑」的分工，这正是 macOS「系统级注册表 vs 用户级偏好」两层模型的体现。

**预期结果**：能复述安装 `.pkg` 后系统依次执行的五步：`killall`（停旧进程）→ `--register-input-source`（注册）→ `--build`（预编译）→ `--enable-input-source`（启用）→ `--select-input-source`（选中）。

> 本实践为源码阅读型，不实际执行（`postinstall` 需要 root 与真实 macOS 安装环境）。

#### 4.3.5 小练习与答案

**练习 1**：如果 `postinstall` 把第 19、20 行的 `sudo -u "${login_user}"` 去掉，直接以 root 跑 `--enable-input-source` / `--select-input-source`，会发生什么？

**答案**：TIS 的 enable/select 是**用户级**操作（写入当前用户的输入法偏好）。以 root 执行会把 Squirrel 启用/选中到 **root 用户**的偏好里，而真正登录的普通用户的输入法列表和当前选中项完全不变——用户在系统设置里看不到 Squirrel 被自动选中，还得手动操作。所以必须用 `sudo -u "${login_user}"` 切到登录用户身份执行。

**练习 2**：[postinstall:L14](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/scripts/postinstall#L14) 用 `[ -z "${RIME_NO_PREBUILD}" ]` 判断是否跳过 `--build`。结合 [Makefile:L169](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L169)，说明这个开关的设计意图。

**答案**：正式发布的 `.pkg` 不设 `RIME_NO_PREBUILD`，安装时会完整预编译方案（`--build`），保证用户首次打字就有缓存、不卡顿；而开发者在本地反复跑 `make install-debug` 自测时，设 `RIME_NO_PREBUILD=1` 跳过耗时的预编译，换取更快的「改一行 → 重装 → 验证」迭代速度。这是「正式发布」与「开发自测」两条路径的分流开关。

---

### 4.4 Sparkle 自动更新：appcast + EdDSA 签名

#### 4.4.1 概念说明

输入法一旦发布，怎么让用户升级到新版本？让用户自己盯着 GitHub Release 显然不友好。Sparkle 是 macOS 上事实标准的第三方自动更新框架（Squirrel 用 submodule 引入，见 u1-l1），它的工作模型很简洁：

1. **定期拉取 appcast**：Sparkle 内部有个定时器，周期性地从 `SUFeedURL` 指定的 URL 下载一个 XML 文件（appcast，本质是 RSS 订阅源），里面列出了「最新版本号 + 下载地址 + 签名」。
2. **比对版本**：把 appcast 里的版本号与当前 App 版本比，若较新就提示用户。
3. **下载并校验签名**：用户同意后下载新版的 `.pkg`（或 `.zip`），**用内置的公钥校验它的 EdDSA 签名**，确认没被中间人篡改。
4. **安装**：校验通过后安装并重启。

这里有两个关键的安全要素，都写在 `Info.plist` 里：

- **`SUFeedURL`**：appcast 的地址——「去哪查更新」。
- **`SUPublicEDKey`**：发布者的 EdDSA（Ed25519）**公钥**——「拿什么验证下载来的更新包是真货」。

为什么需要 `SUPublicEDKey`？因为 appcast 走的是普通 HTTP/HTTPS 下载，Sparkle 不信任传输层，而是要求**每个更新包都用发布者的私钥签名**，客户端用预置的公钥本地校验。这样即便下载源被劫持，攻击者没有私钥也伪造不出能通过校验的包。

签名用的是 Sparkle 自带的 `sign_update` 工具，它读取发布者的私钥，对包计算 EdDSA 签名，输出「签名 + 文件长度」，写进 appcast 的 `<enclosure>` 标签。对应的公钥则用 `generate_keys` 工具生成、写进 `Info.plist` 的 `SUPublicEDKey`。

#### 4.4.2 核心流程

发布侧（开发者发布新版本时）：

```
make archive
   ├─ make package            # 产出已签名公证的 Squirrel.pkg（4.1 + 4.2）
   ├─ 编译 package/sign_update（Sparkle 的签名工具）
   └─ package/make_archive
        ├─ cp Squirrel.pkg → Squirrel-<版本>.pkg
        ├─ ./sign_update <pkg>   # 产出 EdDSA 签名 + 长度
        └─ 生成 appcast.xml（含 sparkle:edSignature、url、length、version）
```

生成出来的 `Squirrel-<版本>.pkg` 上传到 GitHub Release，`appcast.xml` 上传到 `SUFeedURL` 指向的静态站点（`https://rime.github.io/release/squirrel/appcast.xml`）。

客户端侧（用户已装的 Squirrel 运行时）：

```
Squirrel 启动 → SPUStandardUpdaterController 自动启动定时检查
   ├─ 周期性 GET SUFeedURL → 解析 appcast.xml
   ├─ 发现新版本 → 弹通知（见 AppDelegate 的 standardUserDriver 回调）
   ├─ 用户同意 → 下载 enclosure url 指向的 .pkg
   ├─ 用 SUPublicEDKey 校验 sparkle:edSignature
   └─ 校验通过 → 安装（依赖 SUEnableInstallerLauncherService）
```

#### 4.4.3 源码精读

先看 `Info.plist` 里的 Sparkle 配置。

[resources/Info.plist:L104-L113](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L104-L113) 四个键：

- 第 104–105 行 `SUEnableAutomaticChecks = true`：开启自动检查（用户也可在 Sparkle 的偏好里关掉）。
- 第 106–107 行 `SUFeedURL = https://rime.github.io/release/squirrel/appcast.xml`：**更新源的地址**，Sparkle 周期性 GET 它。
- 第 108–109 行 `SUPublicEDKey = ukvWq2dKOWn3B9AsdsQIwOptiDdDKdUjAVNgFxSvB2o=`：**EdDSA 公钥**（Base64），用来校验更新包签名。
- 第 112–113 行 `SUEnableInstallerLauncherService = true`：允许 Sparkle 通过系统服务启动 `.pkg` 安装器（Sparkle 2.x 用它来安装 pkg 类型的更新，而非简单替换文件）。

运行时，Sparkle 由 AppDelegate 持有：

[sources/SquirrelApplicationDelegate.swift:L24](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L24) 创建 `SPUStandardUpdaterController(startingUpdater: true, ...)`——`startingUpdater: true` 表示**一创建就启动**定时检查循环，无需手动 start。它读 `Info.plist` 的 `SUFeedURL` / `SUPublicEDKey` 自动配置。

[sources/SquirrelApplicationDelegate.swift:L101-L108](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L101-L108) 是「手动检查更新」入口：先判 `canCheckForUpdates`（避免在上一次检查未结束时重复触发），再调 `checkForUpdates()`。它由 [SquirrelInputController.swift:L243,L273-L274](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelInputController.swift#L273-L274) 的菜单项「Check for updates...」触发。

更新可用时的用户提示在 [SquirrelApplicationDelegate.swift:L29-L48](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift#L29-L48)——`standardUserDriverWillHandleShowingUpdate` 回调里给 Dock 角标加「1」、发一条系统通知，`standardUserDriverDidReceiveUserAttention` 清掉角标，`standardUserDriverWillFinishUpdateSession` 把激活策略切回 `.accessory`。这套回调是 Sparkle 2.x 的 `SPUStandardUserDriverDelegate` 协议，让宿主 App 能介入更新 UI 的呈现。

现在看发布侧的签名与 appcast 生成。

`sign_update` 工具本身来自 Sparkle 项目，Makefile 里这样编译它：

[Makefile:L126-L128](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L126-L128) 用 `xcodebuild` 编 Sparkle 的 `sign_update` scheme，把产物拷到 `package/`。配套的 `generate_keys`（[Makefile:L122-L124](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L122-L124)）用于首次生成密钥对——私钥留给开发者签名、公钥写进 `Info.plist`。

`make_archive` 用 `sign_update` 对最终包签名并生成 appcast：

[package/make_archive:L33-L44](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/make_archive#L33-L44)：先决定是「校验已有包」还是「新建归档」（受 `checksum` 变量控制，CI 重跑时用于幂等校验），然后第 39 行调 `./sign_update "${target_pkg}"` 算签名。

[package/make_archive:L45-L47](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/make_archive#L45-L47)（紧接着的几行）用 `awk` 从 `sign_update` 的输出里解析出 `edSignature` 和 `length` 两个字段——`sign_update` 的输出形如 `sparkle:edSignature="<签名>" length="<字节数>"`，按双引号分段后第 2、4 段正是签名与长度。

随后脚本用 `cat > appcast.xml << EOF` 把这些值填进 RSS 模板，关键片段（参见 make_archive 生成 `appcast.xml` 的 `enclosure` 标签）：

```xml
<enclosure url="${download_url}"
           sparkle:version="${app_version}"
           sparkle:edSignature="${edSignature}"
           length="${length}"
           type="application/octet-stream"/>
```

客户端 Sparkle 下载 `url` 指向的包后，用 `SUPublicEDKey` 校验 `sparkle:edSignature` 是否与包内容匹配，并核对下载字节数是否等于 `length`——两者都对才安装。`make_archive` 还会额外生成 `testing-appcast.xml`（测试频道）和 `debug-appcast.xml`（本地 file:// 自测频道），方便发布前验证更新链路。

#### 4.4.4 代码实践

**实践目标**：把「`SUFeedURL` 告诉 Sparkle 去哪查、`SUPublicEDKey` 告诉 Sparkle 拿什么验」这条更新信任链的两端对上号，并理解 appcast 里 `edSignature` 与 `length` 的作用。

**操作步骤**：

1. 在 [resources/Info.plist:L106-L109](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/resources/Info.plist#L106-L109) 抄下 `SUFeedURL` 和 `SUPublicEDKey` 的值。
2. 在 [package/make_archive](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/make_archive) 里找到 `enclosure` 标签的生成处（搜索 `sparkle:edSignature`），看清 `edSignature` 和 `length` 两个字段分别来自 `sign_update` 输出的哪一段。
3. 用浏览器或 `curl` 访问 Info.plist 里的 `SUFeedURL`（`https://rime.github.io/release/squirrel/appcast.xml`），观察真实的 appcast 长什么样，找到里面的 `enclosure` 标签。

**需要观察的现象**：你会看到 appcast 是一个 RSS XML，每个 `<item>` 描述一个版本，`<enclosure>` 的 `url` 指向 GitHub Release 的 `.pkg`、`sparkle:edSignature` 是一串 Base64 签名、`length` 是字节数。

**预期结果**：能完整复述「`SUFeedURL` 决定查更新去哪、`SUPublicEDKey` 决定下载的更新包拿什么公钥校验、`edSignature`/`length` 是发布者用私钥对包算出的签名和大小」三者关系。

**待本地验证**：`curl https://rime.github.io/release/squirrel/appcast.xml` 的实际返回内容（取决于该静态站点当前托管版本）。本讲不模拟。

#### 4.4.5 小练习与答案

**练习 1**：如果有人劫持了 `SUFeedURL` 指向的 appcast.xml，把 `enclosure url` 改成自己服务器上的恶意 `.pkg`，Sparkle 会安装这个恶意包吗？为什么？

**答案**：不会。Sparkle 在下载完包后，会用 `Info.plist` 里硬编码的 `SUPublicEDKey` 校验包的 `sparkle:edSignature`。攻击者没有发布者的 EdDSA **私钥**，算不出能通过公钥验证的签名；即便他改了 appcast 里的 `edSignature` 字段，那串签名也和他的恶意包内容对不上，校验失败，Sparkle 拒绝安装。这就是 `SUPublicEDKey` 的核心价值——把信任锚定在 App 内置的公钥上，而非传输通道上。

**练习 2**：[make_archive](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/package/make_archive) 生成了 `appcast.xml`、`testing-appcast.xml`、`debug-appcast.xml` 三个文件，它们有什么区别？发布时用哪个？

**答案**：三者对应不同更新频道：`appcast.xml` 是**正式发布频道**（`SUFeedURL` 默认指向它，`minimumSystemVersion=13.0.0`，所有正式用户收到的更新都来自这里）；`testing-appcast.xml` 是**测试频道**（发布到 `rime.github.io/testing/...`，供愿意尝鲜的用户切换订阅）；`debug-appcast.xml` 是**本地自测频道**（`url` 用 `file://` 指向本地包，`minimumSystemVersion=10.9.0` 放宽，方便开发者在自己的机器上验证整个更新流程而不真正发布）。正式发布用 `appcast.xml`。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，画出 Squirrel 从「源码编译完成」到「用户收到自动更新」的完整发布与安装流水线图，并回答一组串联性问题。

**操作步骤**：

1. 画一张从左到右的流程图，至少包含这些节点与箭头：
   - `Squirrel.app`（编译产物）
   - `codesign`（Developer ID Application）+ `spctl` 自检
   - `pkgbuild` → `Squirrel-component.pkg`
   - `productbuild` → `Squirrel.pkg`
   - `productsign`（Developer ID Installer）+ `notarytool submit --wait` + `stapler staple`
   - `sign_update` → `edSignature` + `length`
   - 上传：`Squirrel-<版本>.pkg` → GitHub Release；`appcast.xml` → SUFeedURL 站点
   - 用户端：Sparkle 拉 appcast → 下载包 → `SUPublicEDKey` 校验 → 安装 → `postinstall` 跑五步
2. 在图上用三种颜色（或标注）区分三类动作：**打包**（4.1）、**签名公证**（4.2）、**Sparkle 更新**（4.4）；并把 **postinstall 五步**（4.3）单独画成用户端安装时的子流程。
3. 回答串联性问题：
   - 如果开发者忘了跑 `sign_update`，appcast 里 `edSignature` 为空，用户端的 Sparkle 会怎样？（提示：校验失败，4.4）
   - 如果用户装的是没设 `DEV_ID` 打出来的 `.pkg`（无签名无公证），安装到 `postinstall` 这一步还会正常注册输入源吗？（提示：postinstall 与签名无关，4.3 能正常跑，但首次运行 App 会被 Gatekeeper 拦）
   - `postinstall` 里的 `--build` 预编译，和 Sparkle 下载新包后的「安装」有什么关系？（提示：Sparkle 装的新包会再次触发 postinstall，于是新版的 `--build` 会重新预编译方案）

**预期结果**：一张完整的「发布 → 分发 → 安装 → 更新」闭环图，并能用本讲学到的概念解释每个节点为什么必须存在、跳过会怎样。

## 6. 本讲小结

- **打包**：`pkgbuild` 把编译产物造成组件包（含 postinstall 脚本与组件属性表），`productbuild` 再用分发脚本包装成带向导 UI 的最终 `Squirrel.pkg`；版本号统一取自 `agvtool` 的 `CURRENT_PROJECT_VERSION`。
- **签名公证**：先用 Developer ID Application 证书 `codesign` 签 App（含 `--options runtime` 硬化运行时），再用 Developer ID Installer 证书 `productsign` 签包，接着 `notarytool submit --wait` 公证、`stapler staple` 装订票据，三者构成 Gatekeeper 放行的流水线；这一切仅在设了 `DEV_ID` 时触发。
- **postinstall 五步**：安装器以 root 身份在铺完文件后执行 `scripts/postinstall`，依次 `killall`（停旧进程，以登录用户身份）→ `--register-input-source`（注册，root）→ `--build`（预编译方案，root，可被 `RIME_NO_PREBUILD` 跳过）→ `--enable-input-source`（以登录用户身份）→ `--select-input-source`（以登录用户身份）；这四条命令复用 Squirrel 的「单二进制双身份」设计。
- **Sparkle 更新**：`Info.plist` 的 `SUFeedURL` 指定 appcast 地址、`SUPublicEDKey` 指定 EdDSA 公钥；发布侧用 `sign_update` 对包签名、生成 appcast 的 `edSignature` 与 `length`；客户端 Sparkle 周期拉 appcast、下载后用公钥本地校验签名，通过才安装——把信任锚定在 App 内置公钥而非传输层。
- **工程分层**：本地自测（`make install-debug` + `RIME_NO_PREBUILD=1` + 不设 `DEV_ID`）与正式发布（`make archive` + `DEV_ID` + 完整签名公证 + 生成 appcast）是两条有意分流的路径，对应不同耗时与安全要求。

## 7. 下一步学习建议

- 本讲聚焦「分发与安装」，如果你想了解 Sparkle 在运行时更细的用户交互（如 Dock 角标、系统通知、静默更新），建议深读 [sources/SquirrelApplicationDelegate.swift](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/sources/SquirrelApplicationDelegate.swift) 的 `SPUStandardUserDriverDelegate` 回调与 `UNUserNotificationCenter` 部分。
- `postinstall` 里的 `--register/enable/select` 真正的实现是 TIS 注册表操作，完整机制见 [u5-l2 输入源注册（TIS）](u5-l2-input-source-registration.md)，建议把两讲对照阅读，理解「脚本调度」与「Swift 实现」两层的关系。
- 若想了解 CI 如何自动化整条发布流水线（包括 `make archive` 的调用、appcast 的部署），可继续阅读 [u5-l6 本地化与 CI](u5-l6-localization-and-ci.md)，并结合仓库根目录的 `action-build.sh` 与 `.github/workflows/` 工作流。
- 打包背后的依赖（librime / plum / Sparkle 子模块）如何就绪，是 [u1-l3 构建与运行](u1-l3-build-and-run.md) 的内容；建议回头看 Makefile 顶部的 `DEPS_CHECK` 四变量与本讲的 `package` 目标如何衔接。
