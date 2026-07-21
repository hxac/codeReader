# 构建与运行

## 1. 本讲目标

学完本讲，你应该能够：

- 看懂 Squirrel 的 `Makefile`，知道敲下 `make` 之后到底发生了什么。
- 说出在执行 `make release` 之前必须先就绪的四件依赖产物（`RIME_LIBRARY`、`PLUM_DATA`、`OPENCC_DATA`、`SPARKLE_FRAMEWORK`），并理解 `DEPS_CHECK` 把它们串起来的作用。
- 区分四条主干目标：构建依赖（`deps`）、编译 App（`release`/`debug`）、安装（`install-release`/`install-debug`）、打包（`package`）。
- 掌握 `ARCHS`、`BUILD_UNIVERSAL`、`DEV_ID`、`MACOSX_DEPLOYMENT_TARGET`、`PLUM_TAG`、`BOOST_ROOT` 等关键环境变量的含义与作用范围。

本讲只读 `Makefile` 与 `INSTALL.md`，不改动任何源码。所有命令都标注「待本地验证」，因为你需要在真正的 macOS 环境里执行。

## 2. 前置知识

### 2.1 为什么 Squirrel 的构建比普通 App 复杂

普通 macOS App 只要把 Swift 源码喂给 Xcode 就能编译出来。但 Squirrel 不一样——它是输入法，输入法把按键转换成汉字这件「苦力活」并不由 Swift 代码完成，而是交给一个 C++ 写的引擎 **librime**。因此 Squirrel 的构建产物里，除了 Swift 编译出的 `Squirrel.app`，还必须包含：

- **librime 动态库**（`librime.1.dylib`）——程序运行时 `dlopen` 调用的引擎。
- **方案与词库数据**（plum/東風破产出的 yaml、essay）——输入法转换需要的规则和词频。
- **简繁转换数据**（OpenCC 的 `.ocd2` 字典）——简繁切换的查表数据。
- **自动更新框架**（`Sparkle.framework`）——App 内的「检查更新」按钮靠它驱动。

这四样东西都不是 Squirrel 仓库自己写的代码，而是来自 git 子模块（librime / plum / Sparkle）和 librime 的依赖（OpenCC）。**Makefile 的核心职责，就是先把这些「外部依赖」准备好，再交给 xcodebuild 编译 App。**

### 2.2 两个会反复用到的 make 概念

1. **目标（target）与依赖（prerequisite）**：`release: $(DEPS_CHECK)` 这一行表示「要构建 `release`，必须先确保 `$(DEPS_CHECK)` 列出的文件都存在」。
2. **基于文件存在性的增量构建**：make 默认只在「目标文件不存在」或「依赖比目标新」时才执行配方（recipe）。Squirrel 把这条规则用在了依赖产物上——`$(RIME_LIBRARY)` 等被写成「文件目标」，只有当 `lib/librime.1.dylib` 等文件缺失时，make 才会触发真正耗时的编译动作。

理解这两点，整份 Makefile 就迎刃而解了。

### 2.3 .gitignore 透露的「产物边界」

在 [u1-l2 仓库目录结构](u1-l2-repo-structure.md) 里我们提到 `bin/`、`lib/`、`Frameworks/`、`data/opencc/`、`data/plum/` 是被忽略的构建产物。本讲会看到，这些目录正是依赖产物落地的地方：

```
Frameworks/*
bin/*
lib/*
data/opencc/
data/plum/
```

Makefile 顶部的四个 `DEPS_CHECK` 变量，对应的几乎都是这些「被忽略」的路径——它们既不会签入版本库，又必须存在才能编译。这条张力是整个构建系统的设计核心。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Makefile](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile) | 构建、依赖、安装、打包的「总调度脚本」，本讲的主角。 |
| [INSTALL.md](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/INSTALL.md) | 面向人类读者的构建指南，列出了前置工具与环境变量。 |
| [scripts/postinstall](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/scripts/postinstall) | `make install-*` 复制完 App 之后会调用的收尾脚本：注册输入源、预编译方案、启用并选中输入法。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块组织：① 依赖构建；② xcodebuild 编译 Squirrel.app；③ 安装与清理目标；④ 构建环境变量。建议按顺序读，因为 ② 依赖 ① 的产物，③ 依赖 ② 的产物，④ 则贯穿 ①②③。

### 4.1 依赖构建：librime / data / sparkle 目标

#### 4.1.1 概念说明

Squirrel 自己的 Swift 代码（`sources/` 下十几个文件）只是「前端壳」。要让它真正能打字，必须先把三块外部依赖编译/拷贝到本地：

- **librime**：引擎本体，产出 `lib/librime.1.dylib`。
- **data**：方案与词库（plum）+ 简繁字典（opencc），产出 `data/plum/*` 与 `data/opencc/*`。
- **sparkle**：自动更新框架，产出 `Frameworks/Sparkle.framework`。

Makefile 用「文件目标」来表达「这件产物是否就绪」：只要目标文件不存在，make 就会触发对应配方去生成它。所有这些产物的文件名被汇总进一个变量 `DEPS_CHECK`，供 `release`/`debug` 目标引用。

#### 4.1.2 核心流程

依赖准备的整体流程可以画成：

```
deps ─┬─ librime ──> $(RIME_DEPS) ──> make -C librime deps
      │           └─> make -C librime release install
      │           └─> copy-rime-binaries  (拷贝 dylib / rime-plugins / 命令行工具)
      │
      └─ data ─┬─ plum-data   ──> make -C plum (+ 可选 PLUM_TAG) ──> copy-plum-data
              └─ opencc-data ──> make -C librime deps/opencc      ──> copy-opencc-data

sparkle ──> git submodule update --init --recursive Sparkle
        ──> xcodebuild Sparkle.xcodeproj ──> copy-sparkle-framework
```

关键点：`deps` 只覆盖 librime 与 data，**不包含 sparkle**。sparkle 由 `$(SPARKLE_FRAMEWORK)` 这个独立的文件目标驱动，在 `release`/`debug` 通过 `DEPS_CHECK` 被间接拉起。所以「一次性把所有依赖备齐」并不是 `make deps` 一条命令，而是依赖 `DEPS_CHECK` 在编译 App 前自动补齐缺失项。

#### 4.1.3 源码精读

先看四个产物变量与汇总变量 `DEPS_CHECK`：

[Makefile:10-26](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L10-L26) —— 这一段定义了四件依赖产物，并把它们汇总进 `DEPS_CHECK`：

```makefile
RIME_LIBRARY_FILE_NAME = librime.1.dylib
RIME_LIBRARY = lib/$(RIME_LIBRARY_FILE_NAME)        # → lib/librime.1.dylib

RIME_DEPS = librime/lib/libmarisa.a ...             # 引擎自身的静态依赖
PLUM_DATA = bin/rime-install \
            data/plum/default.yaml \
            data/plum/symbols.yaml \
            data/plum/essay.txt
OPENCC_DATA = data/opencc/TSCharacters.ocd2 \
              data/opencc/TSPhrases.ocd2 \
              data/opencc/t2s.json
SPARKLE_FRAMEWORK = Frameworks/Sparkle.framework
PACKAGE = package/Squirrel.pkg
DEPS_CHECK = $(RIME_LIBRARY) $(PLUM_DATA) $(OPENCC_DATA) $(SPARKLE_FRAMEWORK)
```

注意 `DEPS_CHECK` 把四件**都**列上了——这正是 `release`/`debug` 编译前的「安检清单」。

接着看 librime 的编译与拷贝：

[Makefile:38-54](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L38-L54) —— librime 的「文件目标」会触发 `make librime`，后者先在 `librime/` 子目录里编 release 并 install，再用 `copy-rime-binaries` 把 dylib、`rime-plugins` 目录、`rime_deployer` 与 `rime_dict_manager` 两个命令行工具拷到仓库根的 `lib/` 与 `bin/`，并用 `install_name_tool -add_rpath @loader_path/../Frameworks` 给这两个工具补上运行期库搜索路径：

```makefile
$(RIME_LIBRARY):
	$(MAKE) librime

librime: $(RIME_DEPS)
	$(MAKE) -C librime release install
	$(MAKE) copy-rime-binaries

copy-rime-binaries:
	cp -L $(RIME_LIB_DIR)/$(RIME_LIBRARY_FILE_NAME) lib/
	cp -pR $(RIME_LIB_DIR)/rime-plugins lib/
	cp $(RIME_BIN_DIR)/rime_deployer bin/
	cp $(RIME_BIN_DIR)/rime_dict_manager bin/
	$(INSTALL_NAME_TOOL) $(INSTALL_NAME_TOOL_ARGS) bin/rime_deployer
	$(INSTALL_NAME_TOOL) $(INSTALL_NAME_TOOL_ARGS) bin/rime_dict_manager
```

> 小提示：`-C librime` 表示「进入 `librime/` 目录执行该子项目的 Makefile」。librime 是一个独立的 CMake 项目，有自己的 Makefile 封装。

再看 data（plum + opencc）：

[Makefile:56-85](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L56-L85) —— `data` 由 `plum-data` 与 `opencc-data` 两部分组成。`plum-data` 先在 plum 子目录里构建，若设置了 `PLUM_TAG` 环境变量（如 `:preset`），就用 plum 自带的 `rime-install` 拉取对应配方，最后 `copy-plum-data` 把产物拷进 `data/plum/`；`opencc-data` 则调用 librime 子项目的 `deps/opencc` 目标再拷贝：

```makefile
plum-data:
	$(MAKE) -C plum
ifdef PLUM_TAG
	rime_dir=plum/output bash plum/rime-install $(PLUM_TAG)
endif
	$(MAKE) copy-plum-data

opencc-data:
	$(MAKE) -C librime deps/opencc
	$(MAKE) copy-opencc-data
```

最后看 sparkle：

[Makefile:112-132](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L112-L132) —— `$(SPARKLE_FRAMEWORK)` 这个文件目标在缺失时会先用 `git submodule update --init --recursive Sparkle` 确保 Sparkle 子模块完整，再用 xcodebuild 编译 Sparkle 自身的工程，最后 `copy-sparkle-framework` 拷到 `Frameworks/`：

```makefile
$(SPARKLE_FRAMEWORK):
	git submodule update --init --recursive Sparkle
	$(MAKE) sparkle

sparkle:
	xcodebuild -project Sparkle/Sparkle.xcodeproj -configuration Release $(BUILD_SETTINGS) build
	$(MAKE) copy-sparkle-framework
```

#### 4.1.4 代码实践

> **实践目标**：亲手验证「`make release` 之前必须先就绪的四件依赖产物」以及 `DEPS_CHECK` 的串联作用。

操作步骤（源码阅读型 + 可选执行）：

1. 打开 [Makefile:26](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L26)，把 `DEPS_CHECK` 拆成四项，分别回溯每项的「文件目标」配方（`$(RIME_LIBRARY)` 在 L38、`$(PLUM_DATA)` 在 L60、`$(OPENCC_DATA)` 在 L63、`$(SPARKLE_FRAMEWORK)` 在 L114）。
2. 在 macOS 上，先执行 `make clean`（清掉产物），再执行 `make release -n`（`-n` 表示 dry-run，只打印不执行）。观察输出里依赖配方与最终 `xcodebuild` 的先后顺序。
3. 再单独执行 `make deps -n`，对比它与 `make release -n` 的差异——你会发现 `make deps` **不含** sparkle 的编译。

需要观察的现象：

- dry-run 输出里，`make -C librime ...`、`make -C plum`、`make -C librime deps/opencc`、`git submodule update ... Sparkle` 都排在 `xcodebuild` 之前。
- 再次执行 `make release -n`（产物已就绪时），依赖配方会消失，只剩 `xcodebuild` 一条。

预期结果：四件产物齐全时，`make release` 直接跳到 xcodebuild；缺任何一件，对应配方会被自动补上。**「待本地验证」**——dry-run 的确切输出依赖你的本机状态。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `make deps` 不能保证 sparkle 也被编译？

**参考答案**：`deps` 目标在 [Makefile:87](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L87) 定义为 `deps: librime data`，只含 librime 与 data，没有 sparkle。sparkle 是通过 `$(SPARKLE_FRAMEWORK)` 文件目标、被 `DEPS_CHECK` 在 `release`/`debug` 编译前间接拉起的。

**练习 2**：`$(RIME_LIBRARY)` 这个变量展开成什么具体路径？它为什么写成「文件目标」而不是 `.PHONY`？

**参考答案**：展开成 `lib/librime.1.dylib`。写成文件目标是为了利用 make 的增量构建——只有当该文件不存在时才触发昂贵的 `make librime`；若改成 `.PHONY`，每次 `make` 都会重新编译整个 librime，浪费时间。

### 4.2 xcodebuild 编译 Squirrel.app

#### 4.2.1 概念说明

依赖就绪后，真正编译 Swift 源码、产出 `Squirrel.app` 的是 Xcode 的命令行工具 `xcodebuild`。Makefile 把它封装进 `release` 与 `debug` 两个目标，对应 Release / Debug 两种构建配置。在 xcodebuild 之前还有两件准备工作：

- `mkdir -p build`：建立 `DERIVED_DATA_PATH`（派生数据目录），让 Xcode 把中间产物放在仓库内、便于定位。
- `bash package/add_data_files`：把 `data/plum`、`data/opencc` 等数据文件登记进 Xcode 工程的资源拷贝清单，使它们能被打进 App 包。

#### 4.2.2 核心流程

```
release / debug
  ├── 1. 校验 DEPS_CHECK 四件产物齐全（make 的依赖机制）
  ├── 2. mkdir -p build            （准备派生数据目录）
  ├── 3. bash package/add_data_files （登记数据文件为 App 资源）
  └── 4. xcodebuild
           -project Squirrel.xcodeproj
           -configuration Release|Debug
           -scheme Squirrel
           -derivedDataPath build
           $(BUILD_SETTINGS)
           build
```

`BUILD_SETTINGS` 是一个「逐项累加」的变量：根据你是否设置了 `ARCHS`、`MACOSX_DEPLOYMENT_TARGET`，它会被追加不同的 `KEY=VALUE`，最终原样传给 xcodebuild。这条机制是 4.4 节环境变量能生效的关键。

#### 4.2.3 源码精读

先看入口别名与目录常量：

[Makefile:1-8](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L1-L8) —— `all` 默认就是 `release`，`install` 就是 `install-release`；同时定义了 librime 产物的目录位置：

```makefile
.PHONY: all install deps release debug

all: release
install: install-release

RIME_BIN_DIR = librime/dist/bin
RIME_LIB_DIR = librime/dist/lib
DERIVED_DATA_PATH = build
```

> 这也解释了 [INSTALL.md](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/INSTALL.md) 里「直接 `make` 就能编译」的写法——`make` 等价于 `make all`，即 `make release`。

再看 release 与 debug 本体：

[Makefile:102-110](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L102-L110) —— 两者结构完全一致，仅 `-configuration` 与最终 `Build/Products/` 子目录不同：

```makefile
release: $(DEPS_CHECK)
	mkdir -p $(DERIVED_DATA_PATH)
	bash package/add_data_files
	xcodebuild -project Squirrel.xcodeproj -configuration Release \
	  -scheme Squirrel -derivedDataPath $(DERIVED_DATA_PATH) $(BUILD_SETTINGS) build

debug: $(DEPS_CHECK)
	mkdir -p $(DERIVED_DATA_PATH)
	bash package/add_data_files
	xcodebuild -project Squirrel.xcodeproj -configuration Debug \
	  -scheme Squirrel -derivedDataPath $(DERIVED_DATA_PATH)  $(BUILD_SETTINGS) build
```

注意第一行 `release: $(DEPS_CHECK)`：这正是 4.1 节那条「安检清单」的接入点。xcodebuild 永远在四件依赖齐全之后才执行。

最后看 `BUILD_SETTINGS` 的累加逻辑（细节留到 4.4 节展开）：

[Makefile:89-100](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L89-L100) —— 用 `ifdef` 判断环境变量是否存在，存在就把对应设置追加进 `BUILD_SETTINGS`，并始终追加 `COMPILER_INDEX_STORE_ENABLE=YES`：

```makefile
ifdef ARCHS
BUILD_SETTINGS += ARCHS="$(ARCHS)"
BUILD_SETTINGS += ONLY_ACTIVE_ARCH=NO
export CMAKE_OSX_ARCHITECTURES = $(subst $(_),;,$(ARCHS))
endif

ifdef MACOSX_DEPLOYMENT_TARGET
BUILD_SETTINGS += MACOSX_DEPLOYMENT_TARGET="$(MACOSX_DEPLOYMENT_TARGET)"
endif

BUILD_SETTINGS += COMPILER_INDEX_STORE_ENABLE=YES
```

#### 4.2.4 代码实践

> **实践目标**：搞清楚 `make`（即 `make release`）到底调用了哪几条命令，以及 `BUILD_SETTINGS` 如何影响 xcodebuild。

操作步骤（源码阅读型）：

1. 在 [Makefile:102](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L102) 处确认 `release` 依赖 `$(DEPS_CHECK)`。
2. 假设你设置了 `export ARCHS='arm64 x86_64'` 与 `export MACOSX_DEPLOYMENT_TARGET='13.0'`，手动把 `BUILD_SETTINGS` 展开成最终字符串（提示：`+=` 会把多项用空格连接）。
3. 用展开后的字符串拼出完整的 `xcodebuild ...` 命令。

需要观察的现象：

- `BUILD_SETTINGS` 展开后形如：`ARCHS="arm64 x86_64" ONLY_ACTIVE_ARCH=NO MACOSX_DEPLOYMENT_TARGET="13.0" COMPILER_INDEX_STORE_ENABLE=YES`。
- 这串会原样接在 `xcodebuild ... build` 之前，成为 build setting 覆盖。

预期结果：你能徒手写出一条带覆盖参数的 xcodebuild 命令，与 Makefile 实际生成的一致。**「待本地验证」**——可以用 `make release -n V=1` 打印真实命令对照。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `release` 与 `debug` 的第一行都是 `$(DEPS_CHECK)` 而不是 `deps`？

**参考答案**：`deps`（L87）只覆盖 librime + data，不含 sparkle；而 `$(DEPS_CHECK)` 是四件产物的完整清单（含 `$(SPARKLE_FRAMEWORK)`）。用 `$(DEPS_CHECK)` 才能保证编译前 sparkle 也已就绪。

**练习 2**：`bash package/add_data_files` 这一步如果省略，会发生什么？

**参考答案**：数据文件（`data/plum`、`data/opencc`）不会被登记进 Xcode 工程的资源拷贝阶段，编译出的 `Squirrel.app` 里会缺少这些数据，输入法运行时找不到方案与字典，无法正常转换。

### 4.3 安装与清理目标

#### 4.3.1 概念说明

编译出 `Squirrel.app` 只是第一步。要让 macOS 真正把它当作输入法来用，还必须：

1. 把 App 拷到系统输入法目录 `/Library/Input Methods/`。
2. 调用 `Squirrel --register-input-source` 让系统 TIS（Text Input Source）登记这个输入法。
3. 预编译用户方案（`--build`）。
4. 启用并选中输入法（`--enable-input-source` / `--select-input-source`）。

Makefile 把「拷贝 + 收尾」封装成 `install-release` / `install-debug`；把「制作可分发的 .pkg 安装包」封装成 `package`；把「删产物重新开始」封装成 `clean` / `clean-package` / `clean-deps`。

#### 4.3.2 核心流程

```
install-release / install-debug
  ├── release / debug                 （先编译，复用 4.2）
  ├── permission-check                （确保对目标目录有写权限）
  ├── rm -rf + cp -R 到 /Library/Input Methods/Squirrel.app
  └── bash scripts/postinstall        （收尾脚本）
        ├── killall Squirrel
        ├── Squirrel --register-input-source
        ├── Squirrel --build          （install-release 才有；install-debug 用 RIME_NO_PREBUILD=1 跳过）
        ├── Squirrel --enable-input-source
        └── Squirrel --select-input-source

package: release + $(PACKAGE)
  ├── (可选) sign_app        （需 DEV_ID）
  ├── make_package            （pkgbuild / productbuild 打 .pkg）
  └── (可选) productsign + notarytool + stapler   （签名 + 公证 + 装订）
```

清理目标分三层，粒度由细到粗：`clean` 只删 Squirrel 自身产物，`clean-package` 只删打包产物，`clean-deps` 才会动 librime/plum/sparkle 这类重依赖。

#### 4.3.3 源码精读

先看目标路径常量与权限检查：

[Makefile:158-164](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L158-L164) —— 目标目录是 `/Library/Input Methods`，App 根是其中的 `Squirrel.app`；`permission-check` 在用户对该目录无写权限时用 `sudo chown` 修正归属：

```makefile
DSTROOT = /Library/Input Methods
SQUIRREL_APP_ROOT = $(DSTROOT)/Squirrel.app

permission-check:
	[ -w "$(DSTROOT)" ] && [ -w "$(SQUIRREL_APP_ROOT)" ] || sudo chown -R ${USER} "$(DSTROOT)"
```

两个安装目标：

[Makefile:166-174](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L166-L174) —— `install-debug` 与 `install-release` 都先依赖对应编译目标、做权限检查，再删旧 App、拷新 App，最后跑 `postinstall`。唯一差异：`install-debug` 给 postinstall 传了 `RIME_NO_PREBUILD=1`，跳过耗时的方案预编译（调试时反复安装很省时间）：

```makefile
install-debug: debug permission-check
	rm -rf "$(SQUIRREL_APP_ROOT)"
	cp -R $(DERIVED_DATA_PATH)/Build/Products/Debug/Squirrel.app "$(DSTROOT)"
	DSTROOT="$(DSTROOT)" RIME_NO_PREBUILD=1 bash scripts/postinstall

install-release: release permission-check
	rm -rf "$(SQUIRREL_APP_ROOT)"
	cp -R $(DERIVED_DATA_PATH)/Build/Products/Release/Squirrel.app "$(DSTROOT)"
	DSTROOT="$(DSTROOT)" bash scripts/postinstall
```

postinstall 脚本本体：

[scripts/postinstall:10-21](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/scripts/postinstall#L10-L21) —— 先 kill 旧进程，再注册输入源；只有 `RIME_NO_PREBUILD` 未设置时才 `--build`；最后启用并选中输入法：

```bash
/usr/bin/sudo -u "${login_user}" /usr/bin/killall Squirrel > /dev/null || true

"${squirrel_executable}" --register-input-source

if [ -z "${RIME_NO_PREBUILD}" ]; then
    pushd "${rime_shared_data_path}" > /dev/null
    "${squirrel_executable}" --build
    popd > /dev/null
fi && (
    /usr/bin/sudo -u "${login_user}" "${squirrel_executable}" --enable-input-source
    /usr/bin/sudo -u "${login_user}" "${squirrel_executable}" --select-input-source
)
```

> 这里出现的 `--register-input-source` / `--build` 等命令行参数，正是 [u1-l4 程序入口与启动流程](u1-l4-entry-and-startup.md) 里 `Main.swift` 双分支中的「命令行分支」。本讲只需知道它们被 postinstall 调用即可。

打包目标：

[Makefile:140-153](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L140-L153) —— `$(PACKAGE)` 在设置了 `DEV_ID` 时先签名 App（`sign_app`），再用 `make_package` 打包；若有 `DEV_ID` 还会 `productsign` 签安装包、`notarytool submit --wait` 公证、`stapler staple` 装订公证票据。`package` 目标先依赖 `release`，再构建 `$(PACKAGE)`：

```makefile
$(PACKAGE):
ifdef DEV_ID
	bash package/sign_app "$(DEV_ID)" "$(DERIVED_DATA_PATH)"
endif
	bash package/make_package "$(DERIVED_DATA_PATH)"
ifdef DEV_ID
	productsign --sign "Developer ID Installer: $(DEV_ID)" package/Squirrel.pkg package/Squirrel-signed.pkg
	# ... 公证与装订 ...
endif

package: release $(PACKAGE)
```

三层清理：

[Makefile:178-196](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L178-L196) —— `clean` 删 `build/`、`bin/*`、`lib/*`、`data/plum/*`、`data/opencc/*` 等 Squirrel 自身产物；`clean-package` 删 `.pkg`、`sign_update`、`*appcast.xml`；`clean-deps` 才进入 `plum/`、`librime/` 子目录执行它们的 clean，并删 `librime/dist`、调用 `clean-sparkle`：

```makefile
clean:
	rm -rf build > /dev/null 2>&1 || true
	rm bin/* > /dev/null 2>&1 || true
	rm lib/* > /dev/null 2>&1 || true
	rm data/plum/* > /dev/null 2>&1 || true
	rm data/opencc/* > /dev/null 2>&1 || true

clean-package:
	rm -rf package/*appcast.xml > /dev/null 2>&1 || true
	rm -rf package/*.pkg > /dev/null 2>&1 || true

clean-deps:
	$(MAKE) -C plum clean
	$(MAKE) -C librime clean
	rm -rf librime/dist > /dev/null 2>&1 || true
	$(MAKE) clean-sparkle
```

这与 [INSTALL.md:160-182](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/INSTALL.md#L160-L182) 给人类读者的「清理建议」一一对应：先 `make clean`，不行再 `make clean-deps`，打包问题用 `make clean-package`。

#### 4.3.4 代码实践

> **实践目标**：读懂「安装一个可运行的 Squirrel」需要经过的完整命令序列，并理解三个 clean 目标的边界。

操作步骤（源码阅读型）：

1. 从 [Makefile:171](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L171) 的 `install-release` 出发，列出它依次执行的命令：`make release` → `permission-check` → `rm -rf` → `cp -R` → `bash scripts/postinstall`。
2. 再展开 [scripts/postinstall:10-21](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/scripts/postinstall#L10-L21)，列出 postinstall 内部四条 Squirrel 子命令。
3. 对照三个 clean 目标，判断「只想重新编译 App，不重编 librime」应该用哪一个。

需要观察的现象：

- 安装序列里 `--register-input-source` 一定先于 `--build`，`--build` 先于 `--enable-input-source`/`--select-input-source`。
- 「只想重新编译 App」应使用 `make clean`（它不碰 librime/plum/sparkle）。

预期结果：你能写出 install-release 的完整命令链，并能正确选择 clean 目标。**「待本地验证」**——真实安装需要 macOS 与 sudo。

#### 4.3.5 小练习与答案

**练习 1**：`install-debug` 为什么给 postinstall 传 `RIME_NO_PREBUILD=1`？

**参考答案**：调试时往往会反复编译、反复安装。每次安装都跑一遍 `--build`（预编译用户方案）非常耗时。`RIME_NO_PREBUILD=1` 让 postinstall 跳过 [scripts/postinstall:14-18](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/scripts/postinstall#L14-L18) 的预编译分支，缩短调试循环。release 安装则必须预编译，否则用户首次使用会很卡。

**练习 2**：`package` 与 `install-release` 都依赖 `release`，它们的产出有何不同？

**参考答案**：`install-release` 把 App 直接拷进本机 `/Library/Input Methods/` 并注册启用，适合本机试用；`package` 则把 App 打包成一个可分发的 `Squirrel.pkg` 安装包（可选签名公证），供其他用户安装，不会改动本机系统目录。

### 4.4 构建环境变量

#### 4.4.1 概念说明

Makefile 通过「环境变量 + `ifdef` 追加」的方式，让你在不改 Makefile 的前提下定制构建。这些变量大致分三类：

| 变量 | 作用 | 影响范围 |
| --- | --- | --- |
| `ARCHS` | 指定目标架构，如 `arm64 x86_64` | xcodebuild 与 librime 的 CMake（经 `CMAKE_OSX_ARCHITECTURES`）|
| `BUILD_UNIVERSAL` | 告知「按通用二进制构建」（主要用于 Boost/librime）| librime 子项目的依赖编译 |
| `MACOSX_DEPLOYMENT_TARGET` | 最低支持系统版本，如 `13.0` | xcodebuild 与 CMake |
| `DEV_ID` | Apple 开发者 ID 名称，用于签名与公证 | `package` 目标 |
| `PLUM_TAG` | plum 配方集合，如 `:preset` / `:extra` | `plum-data` 目标 |
| `BOOST_ROOT` | Boost 源码根目录，编译 librime 必需 | librime 子项目 |

这些变量既可以在 shell 里 `export`，也可以作为参数追加在 `make` 命令后，例如 `make ARCHS='arm64 x86_64'`。

#### 4.4.2 核心流程

变量生效的路径有两条：

1. **进入 `BUILD_SETTINGS`**：`ARCHS`、`MACOSX_DEPLOYMENT_TARGET` 经 `ifdef` 判断后追加进 `BUILD_SETTINGS`，最终随 `xcodebuild` 传给 Xcode；`ARCHS` 还会同时 `export` 成 `CMAKE_OSX_ARCHITECTURES`（分号分隔），供 librime 的 CMake 读取。
2. **直接被特定目标读取**：`DEV_ID` 只在 `$(PACKAGE)` 里被 `ifdef`；`PLUM_TAG` 只在 `plum-data` 里被 `ifdef`；`BUILD_UNIVERSAL`/`BOOST_ROOT` 主要在 librime 子项目内部使用（INSTALL.md 描述了它们的用法）。

```
ARCHS ── ifdef ──> BUILD_SETTINGS += ARCHS=... + ONLY_ACTIVE_ARCH=NO
                └─> export CMAKE_OSX_ARCHITECTURES = arm64;x86_64  (subst 把空格换成分号)

MACOSX_DEPLOYMENT_TARGET ── ifdef ──> BUILD_SETTINGS += MACOSX_DEPLOYMENT_TARGET=...

DEV_ID ── ifdef (仅 $(PACKAGE)) ──> sign_app + productsign + notarytool + stapler
PLUM_TAG ── ifdef (仅 plum-data) ──> rime-install $(PLUM_TAG)
```

#### 4.4.3 源码精读

ARCHS 与 MACOSX_DEPLOYMENT_TARGET 的累加：

[Makefile:89-98](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L89-L98) —— 注意 L92 的 `_=$() $()` 是一个生成「单个空格」的 make 技巧，配合 L93 的 `$(subst $(_),;,$(ARCHS))` 把 `'arm64 x86_64'` 转成 `arm64;x86_64`（CMake 期望的分号分隔格式）：

```makefile
ifdef ARCHS
BUILD_SETTINGS += ARCHS="$(ARCHS)"
BUILD_SETTINGS += ONLY_ACTIVE_ARCH=NO
_=$() $()
export CMAKE_OSX_ARCHITECTURES = $(subst $(_),;,$(ARCHS))
endif

ifdef MACOSX_DEPLOYMENT_TARGET
BUILD_SETTINGS += MACOSX_DEPLOYMENT_TARGET="$(MACOSX_DEPLOYMENT_TARGET)"
endif
```

DEV_ID 在打包阶段的作用：

[Makefile:140-151](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L140-L151) —— `ifdef DEV_ID` 同时控制三件事：签名 App（`sign_app`）、签名安装包（`productsign`）、公证与装订（`notarytool` + `stapler`）。未设置 `DEV_ID` 时，这些步骤全部跳过，只产出未签名的 `.pkg`：

```makefile
$(PACKAGE):
ifdef DEV_ID
	bash package/sign_app "$(DEV_ID)" "$(DERIVED_DATA_PATH)"
endif
	bash package/make_package "$(DERIVED_DATA_PATH)"
ifdef DEV_ID
	productsign --sign "Developer ID Installer: $(DEV_ID)" package/Squirrel.pkg package/Squirrel-signed.pkg
	rm package/Squirrel.pkg
	mv package/Squirrel-signed.pkg package/Squirrel.pkg
	xcrun notarytool submit package/Squirrel.pkg --keychain-profile "$(DEV_ID)" --wait
	xcrun stapler staple package/Squirrel.pkg
endif
```

PLUM_TAG 在 plum-data 中的作用：

[Makefile:66-71](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L66-L71) —— 只有设置了 `PLUM_TAG`，才会用 plum 的 `rime-install` 拉取对应配方集：

```makefile
plum-data:
	$(MAKE) -C plum
ifdef PLUM_TAG
	rime_dir=plum/output bash plum/rime-install $(PLUM_TAG)
endif
	$(MAKE) copy-plum-data
```

INSTALL.md 对人类读者的完整变量清单：

[INSTALL.md:103-112](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/INSTALL.md#L103-L112) —— 这是文档侧的「权威变量表」，明确写出 `BOOST_ROOT` 必填、`DEV_ID`/`BUILD_UNIVERSAL`/`PLUM_TAG`/`ARCHS`/`MACOSX_DEPLOYMENT_TARGET` 可选，并提示低于 13.0 的部署目标未经测试：

```sh
export BOOST_ROOT="path_to_boost"           # required
export DEV_ID="Your Apple ID name"          # include this to codesign, optional
export BUILD_UNIVERSAL=1                     # set to build universal binary
export PLUM_TAG=":preset"                    # or ":extra", optional
export ARCHS='arm64 x86_64'                  # optional
export MACOSX_DEPLOYMENT_TARGET='13.0'       # optional
```

[INSTALL.md:122-125](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/INSTALL.md#L122-L125) 给出了「作为 make 参数」的等价写法：

```sh
# for Universal macOS App
make ARCHS='arm64 x86_64' BUILD_UNIVERSAL=1
```

#### 4.4.4 代码实践

> **实践目标**：验证 `ARCHS` 如何同时影响 xcodebuild 与 librime 的 CMake。

操作步骤（源码阅读型）：

1. 假设 `ARCHS='arm64 x86_64'`，在 [Makefile:89-94](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L89-L94) 手动展开：
   - `BUILD_SETTINGS` 追加的两项是什么？
   - `CMAKE_OSX_ARCHITECTURES` 被 `export` 成什么值？（提示：`$(subst $(_),;,...)` 把空格替换为分号）
2. 思考：为什么 librime 需要单独的 `CMAKE_OSX_ARCHITECTURES`，而不能只靠 xcodebuild 的 `ARCHS`？

需要观察的现象：

- `CMAKE_OSX_ARCHITECTURES` 展开为 `arm64;x86_64`（分号分隔），这是 CMake `OSX_ARCHITECTURES` 的标准格式。
- 因为 librime 是用 CMake 提前编译成 dylib 的，它的架构在 librime 编译阶段就定型了；xcodebuild 的 `ARCHS` 只管 Swift 代码。若两者不一致，会出现「dylib 是单架构、App 是双架构」的链接失败。

预期结果：你能解释「为什么构建通用二进制必须让 `ARCHS` 与 `CMAKE_OSX_ARCHITECTURES` 保持一致」。**「待本地验证」**——可在 macOS 上用 `make ARCHS='arm64 x86_64' release -n` 观察导出的环境变量。

#### 4.4.5 小练习与答案

**练习 1**：不设置 `DEV_ID` 时，`make package` 产出的 `.pkg` 能否直接分发给其他用户？

**参考答案**：能分发，但**未签名、未公证**。macOS Gatekeeper 会拦截未签名的安装包，用户需要手动在「系统设置 → 隐私与安全性」里允许打开。设置 `DEV_ID` 后，[Makefile:140-151](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/Makefile#L140-L151) 会自动完成签名、公证、装订，用户双击即可安装。

**练习 2**：`_=$() $()` 这一行看起来很怪，它到底在做什么？

**参考答案**：`$()` 是空字符串，`$() $()` 是「两个空字符串中间一个空格」，所以 `_` 被赋值为一个**单个空格**。下一行 `$(subst $(_),;,$(ARCHS))` 用这个空格作为分隔符，把 `'arm64 x86_64'` 里的空格替换成 `;`，得到 CMake 期望的 `arm64;x86_64`。这是 make 里「定义一个空格变量」的经典技巧。

## 5. 综合实践

> **贯穿任务**：你是新接手 Squirrel 的构建维护者，需要从零把项目跑起来，并回答三个问题。

任务步骤：

1. **拉取子模块**：按 [INSTALL.md:99-101](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/INSTALL.md#L99-L101) 执行 `git submodule update --init --recursive`，确认 `librime/`、`plum/`、`Sparkle/` 三个子模块都已拉取。
2. **备依赖**（任选一条路径）：
   - 完整路径：按 [INSTALL.md:55-73](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/INSTALL.md#L55-L73) 装 Boost、编 librime；或
   - 快捷路径：按 [INSTALL.md:44-53](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/INSTALL.md#L44-L53) 执行 `bash ./action-install.sh` 直接下载 librime 预编译产物。
3. **编译**：执行 `make`（等价 `make release`），观察 dry-run 里依赖配方与 xcodebuild 的先后顺序（`make release -n`）。
4. **回答三个问题**：
   - 在 `make release` 真正执行 `xcodebuild` 之前，`DEPS_CHECK` 保证了哪四个文件/目录存在？
   - 想同时支持 Apple Silicon 与 Intel，应该设置哪两个变量？为什么必须同时设置？
   - `make install-release` 与 `make package` 的产出分别落到哪里、面向什么场景？

预期结果：你能徒手画出「子模块 → 依赖产物（DEPS_CHECK）→ xcodebuild → Squirrel.app → install/package」这条完整链路，并能解释每个环节的输入与输出。**「待本地验证」**——整个流程必须在 macOS 13.0+ 与 Xcode 14.0+ 上执行（见 [INSTALL.md:7-14](https://github.com/rime/squirrel/blob/2158538755542b11964655e2f9606ba4a066edfe/INSTALL.md#L7-L14)）。

## 6. 本讲小结

- Squirrel 的构建本质是「**先备四件外部依赖，再编译 Swift 前端**」，Makefile 用 `DEPS_CHECK`（`RIME_LIBRARY`/`PLUM_DATA`/`OPENCC_DATA`/`SPARKLE_FRAMEWORK`）做这道安检。
- 依赖来自三个 git 子模块（librime/plum/Sparkle）与 librime 的依赖（OpenCC），落地到被 `.gitignore` 忽略的 `bin/`、`lib/`、`Frameworks/`、`data/plum/`、`data/opencc/`。
- `release`/`debug` 通过 `xcodebuild` 编译 `Squirrel.app`，编译前用 `mkdir -p build` 与 `bash package/add_data_files` 做两项准备；`BUILD_SETTINGS` 用 `ifdef +=` 累加环境变量。
- `install-release`/`install-debug` 把 App 拷进 `/Library/Input Methods/` 并跑 `scripts/postinstall`（注册→预编译→启用→选中输入法）；`package` 打出可分发的 `.pkg`，`DEV_ID` 决定是否签名公证。
- 清理分三层：`clean`（Squirrel 产物）、`clean-package`（打包产物）、`clean-deps`（重依赖 librime/plum/sparkle）。
- 关键环境变量：`ARCHS`（同时驱动 xcodebuild 与 `CMAKE_OSX_ARCHITECTURES`）、`BUILD_UNIVERSAL`、`MACOSX_DEPLOYMENT_TARGET`、`DEV_ID`、`PLUM_TAG`、`BOOST_ROOT`。

## 7. 下一步学习建议

本讲解决了「怎么把 Squirrel 编译并装到机器上」。编译产物里的 `Squirrel.app` 真正运行时，入口是 `sources/Main.swift`。建议接着学习：

- [u1-l4 程序入口与启动流程](u1-l4-entry-and-startup.md)：精读 `SquirrelApp.main()`，理解命令行分支（`--register-input-source`/`--build` 等，正是本讲 postinstall 调用的那些）与正常输入法启动分支。
- [u1-l5 macOS 输入法（IMK）基础概念](u1-l5-imk-input-method-concepts.md)：理解本讲「注册/启用/选中输入法」背后 macOS IMK 与 TIS 的机制。
- 进阶后可回看 [u5-l5 打包、安装与 Sparkle 更新](u5-l5-packaging-installer.md)，深入 `package/sign_app`、`package/make_package` 与 Sparkle 的 `SUFeedURL`/`SUPublicEDKey` 自动更新细节。
