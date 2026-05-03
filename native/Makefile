# Build umbra LD_PRELOAD shim.
#
#   make           → libumbra_shim.so (release, stripped)
#   make debug     → with debug symbols
#   make verbose   → also adds -DUMBRA_SHIM_FORCE_VERBOSE
#   make clean     → remove build artifacts
#   make test      → run the smoke test (requires built shim)

CC      ?= cc
CFLAGS  ?= -O2 -fPIC -Wall -Wextra -fvisibility=hidden
LDFLAGS ?= -shared -ldl

TARGET   := libumbra_shim.so
SOURCES  := shim.c

.PHONY: all debug verbose clean test

all: $(TARGET)

$(TARGET): $(SOURCES)
	$(CC) $(CFLAGS) $(SOURCES) -o $@ $(LDFLAGS)
	strip --strip-unneeded $@
	@echo "built: $$(realpath $@) ($$(stat -c %s $@) bytes)"

debug: CFLAGS += -O0 -g -DDEBUG
debug: $(TARGET)
	@echo "debug build (no strip)"

verbose: CFLAGS += -DUMBRA_SHIM_FORCE_VERBOSE
verbose: $(TARGET)

test: $(TARGET)
	UMBRA_SHIM_VERBOSE=1 LD_PRELOAD=$$(realpath $(TARGET)) /bin/echo "shim loaded ok"

clean:
	rm -f $(TARGET) *.o
