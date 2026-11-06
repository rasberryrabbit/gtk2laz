#!/usr/bin/env python3
"""
gen_fake_includes.py
====================
Generate a complete set of fake libc / GTK stub headers for pycparser.

These stubs declare just enough types and function prototypes that
pycparser's `parse_file(..., use_cpp=True)` succeeds on a typical
GTK application without dragging in the real system headers (which
would blow up the AST with thousands of internal symbols).
"""
import os
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fake_libc_include"

HEADERS = {
    "stdint.h": """#ifndef _STDINT_H
#define _STDINT_H
typedef int int8_t;
typedef int int16_t;
typedef int int32_t;
typedef long int64_t;
typedef unsigned int uint8_t;
typedef unsigned int uint16_t;
typedef unsigned int uint32_t;
typedef unsigned long uint64_t;
typedef unsigned long uintptr_t;
typedef long intptr_t;
#endif
""",
    "stdio.h": """#ifndef _STDIO_H
#define _STDIO_H
typedef struct _IO_FILE FILE;
extern int printf(const char *, ...);
extern int fprintf(FILE *, const char *, ...);
extern int sprintf(char *, const char *, ...);
extern int snprintf(char *, unsigned long, const char *, ...);
extern FILE *fopen(const char *, const char *);
extern int fclose(FILE *);
extern unsigned long fread(void *, unsigned long, unsigned long, FILE *);
extern unsigned long fwrite(const void *, unsigned long, unsigned long, FILE *);
extern int fseek(FILE *, long, int);
extern long ftell(FILE *);
extern int feof(FILE *);
extern char *fgets(char *, int, FILE *);
extern int fputs(const char *, FILE *);
extern int fgetc(FILE *);
extern int fputc(int, FILE *);
#endif
""",
    "stdlib.h": """#ifndef _STDLIB_H
#define _STDLIB_H
typedef unsigned long size_t;
extern void *malloc(size_t);
extern void *calloc(size_t, size_t);
extern void *realloc(void *, size_t);
extern void free(void *);
extern void exit(int);
extern int atoi(const char *);
extern long strtol(const char *, char **, int);
extern double atof(const char *);
#endif
""",
    "string.h": """#ifndef _STRING_H
#define _STRING_H
typedef unsigned long size_t;
extern char *strcpy(char *, const char *);
extern char *strncpy(char *, const char *, size_t);
extern char *strcat(char *, const char *);
extern char *strncat(char *, const char *, size_t);
extern int strcmp(const char *, const char *);
extern int strncmp(const char *, const char *, size_t);
extern int strcasecmp(const char *, const char *);
extern int strncasecmp(const char *, const char *, size_t);
extern size_t strlen(const char *);
extern char *strchr(const char *, int);
extern char *strrchr(const char *, int);
extern char *strstr(const char *, const char *);
extern char *strdup(const char *);
extern void *memcpy(void *, const void *, size_t);
extern void *memmove(void *, const void *, size_t);
extern void *memset(void *, int, size_t);
extern int memcmp(const void *, const void *, size_t);
#endif
""",
    "unistd.h": """#ifndef _UNISTD_H
#define _UNISTD_H
extern int close(int);
extern long read(int, void *, unsigned long);
extern long write(int, const void *, unsigned long);
extern int access(const char *, int);
extern int unlink(const char *);
extern unsigned int sleep(unsigned int);
extern int usleep(unsigned int);
#endif
""",
    "time.h": """#ifndef _TIME_H
#define _TIME_H
typedef long time_t;
struct tm { int tm_sec; int tm_min; int tm_hour; int tm_mday; int tm_mon; int tm_year; };
struct timespec { long tv_sec; long tv_nsec; };
struct timeval { long tv_sec; long tv_usec; };
extern time_t time(time_t *);
extern struct tm *localtime(const time_t *);
extern int clock_gettime(int, struct timespec *);
extern int gettimeofday(struct timeval *, void *);
#define CLOCK_REALTIME 0
#endif
""",
    "sys/select.h": """#ifndef _SYS_SELECT_H
#define _SYS_SELECT_H
typedef struct { unsigned long fds_bits[8]; } fd_set;
extern void FD_ZERO(fd_set *);
extern void FD_SET(int, fd_set *);
extern void FD_CLR(int, fd_set *);
extern int FD_ISSET(int, fd_set *);
#endif
""",
    "ctype.h": """#ifndef _CTYPE_H
#define _CTYPE_H
extern int isdigit(int);
extern int isalpha(int);
extern int isalnum(int);
extern int isspace(int);
extern int isupper(int);
extern int islower(int);
extern int tolower(int);
extern int toupper(int);
#endif
""",
    "getopt.h": """#ifndef _GETOPT_H
#define _GETOPT_H
struct option { const char *name; int has_arg; int *flag; int val; };
extern int getopt_long(int, char *const *, const char *, const struct option *, int *);
extern int getopt(int, char *const *, const char *);
#endif
""",
    "fcntl.h": """#ifndef _FCNTL_H
#define _FCNTL_H
extern int open(const char *, int, ...);
#define O_RDONLY 0
#define O_WRONLY 1
#define O_RDWR 2
#define O_CREAT 0x40
#define O_TRUNC 0x80
#define O_APPEND 0x400
#endif
""",
    "errno.h": """#ifndef _ERRNO_H
#define _ERRNO_H
extern int errno;
#define EAGAIN 11
#define EBADF 9
#define EINVAL 22
#define ENOENT 2
#define ENODATA 61
#endif
""",
    "wchar.h": """#ifndef _WCHAR_H
#define _WCHAR_H
typedef int wchar_t;
typedef unsigned long wint_t;
#endif
""",
    "math.h": """#ifndef _MATH_H
#define _MATH_H
extern double sqrt(double);
extern double pow(double, double);
extern double floor(double);
extern double ceil(double);
extern double round(double);
#endif
""",
    "sys/types.h": """#ifndef _SYS_TYPES_H
#define _SYS_TYPES_H
typedef long ssize_t;
typedef unsigned long size_t;
typedef int pid_t;
typedef int uid_t;
typedef int gid_t;
typedef int mode_t;
typedef long off_t;
typedef long time_t;
typedef unsigned long uintptr_t;
typedef long intptr_t;
#endif
""",
    "sys/stat.h": """#ifndef _SYS_STAT_H
#define _SYS_STAT_H
struct stat { long st_size; int st_mode; long st_mtime; };
extern int stat(const char *, struct stat *);
extern int fstat(int, struct stat *);
#define S_IFMT 0xF000
#define S_IFDIR 0x4000
#define S_IFREG 0x8000
#endif
""",
    "sys/ioctl.h": """#ifndef _SYS_IOCTL_H
#define _SYS_IOCTL_H
extern int ioctl(int, unsigned long, ...);
#endif
""",
    "sys/timeb.h": """#ifndef _SYS_TIMEB_H
#define _SYS_TIMEB_H
struct timeb { long time; unsigned short millitm; short timezone; short dstflag; };
extern int ftime(struct timeb *);
#endif
""",
    "linux/hiddev.h": """#ifndef _LINUX_HIDDEV_H
#define _LINUX_HIDDEV_H
struct hiddev_devinfo { int bustype; int busnum; int devnum; int num_applications; };
struct hiddev_collection_info { int type; int usage; int level; };
struct hiddev_report_info { int report_type; int report_id; int num_fields; };
struct hiddev_field_info { int report_type; int report_id; int field_index; int maxusage; };
struct hiddev_usage_ref { int report_type; int report_id; int field_index; int usage_index; int usage_code; int value; };
struct hiddev_usage_ref_multi { struct hiddev_usage_ref uref; int num_values; int values[1024]; };
#define HIDIOCGDEVINFO 0x48004001
#define HIDIOCGCOLLECTIONINFO 0x48004002
#define HIDIOCGREPORT 0x48004003
#define HIDIOCGREPORTINFO 0x48004004
#define HIDIOCGFIELDINFO 0x48004005
#define HIDIOCGUSAGE 0x48004006
#define HIDIOCSUSAGE 0x48004007
#endif
""",
    "linux/hidraw.h": """#ifndef _LINUX_HIDRAW_H
#define _LINUX_HIDRAW_H
#define HIDRAW_FIRST_MINOR 0
#define HIDRAW_MAX_DEVICES 64
#define HIDIOCGRDESCSIZE 0x48044001
#define HIDIOCGRDESC 0x90044802
#define HIDIOCGRAWINFO 0x80084803
#define HIDIOCGRAWNAME(len) 0x81044804
#endif
""",
    "linux/input.h": """#ifndef _LINUX_INPUT_H
#define _LINUX_INPUT_H
struct input_event { long tv_sec; long tv_usec; unsigned short type; unsigned short code; int value; };
#endif
""",
    "asm/types.h": """#ifndef _ASM_TYPES_H
#define _ASM_TYPES_H
typedef unsigned int __u32;
typedef unsigned short __u16;
typedef unsigned char __u8;
typedef int __s32;
typedef short __s16;
typedef signed char __s8;
#endif
""",
    "gtk/gtk.h": """#ifndef __GTK_H__
#define __GTK_H__
typedef struct _GtkWidget GtkWidget;
typedef struct _GtkWindow GtkWindow;
typedef struct _GtkButton GtkButton;
typedef struct _GtkLabel GtkLabel;
typedef struct _GtkEntry GtkEntry;
typedef struct _GtkBox GtkBox;
typedef struct _GtkNotebook GtkNotebook;
typedef struct _GtkComboBox GtkComboBox;
typedef struct _GtkComboBoxText GtkComboBoxText;
typedef struct _GtkSpinButton GtkSpinButton;
typedef struct _GtkTextView GtkTextView;
typedef struct _GtkTextBuffer GtkTextBuffer;
typedef struct _GtkTextIter GtkTextIter;
typedef struct _GtkStatusbar GtkStatusbar;
typedef struct _GtkTreeView GtkTreeView;
typedef struct _GtkTreeSelection GtkTreeSelection;
typedef struct _GtkListStore GtkListStore;
typedef struct _GtkTreeStore GtkTreeStore;
typedef struct _GtkTreeIter GtkTreeIter;
typedef struct _GtkTreePath GtkTreePath;
typedef struct _GtkTreeModel GtkTreeModel;
typedef struct _GtkCellRenderer GtkCellRenderer;
typedef struct _GtkCellRendererText GtkCellRendererText;
typedef struct _GtkCellRendererPixbuf GtkCellRendererPixbuf;
typedef struct _GtkTreeViewColumn GtkTreeViewColumn;
typedef struct _GtkFileChooser GtkFileChooser;
typedef struct _GtkFileChooserDialog GtkFileChooserDialog;
typedef struct _GtkDialog GtkDialog;
typedef struct _GtkMessageDialog GtkMessageDialog;
typedef struct _GtkScale GtkScale;
typedef struct _GtkHScale GtkHScale;
typedef struct _GtkVScale GtkVScale;
typedef struct _GtkScrollbar GtkScrollbar;
typedef struct _GtkHScrollbar GtkHScrollbar;
typedef struct _GtkVScrollbar GtkVScrollbar;
typedef struct _GtkPaned GtkPaned;
typedef struct _GtkHPaned GtkHPaned;
typedef struct _GtkVPaned GtkVPaned;
typedef struct _GtkSeparator GtkSeparator;
typedef struct _GtkFrame GtkFrame;
typedef struct _GtkAspectFrame GtkAspectFrame;
typedef struct _GtkScrolledWindow GtkScrolledWindow;
typedef struct _GtkDrawingArea GtkDrawingArea;
typedef struct _GtkToggleToolButton GtkToggleToolButton;
typedef struct _GtkToggleButton GtkToggleButton;
typedef struct _GtkCheckButton GtkCheckButton;
typedef struct _GtkRadioButton GtkRadioButton;
typedef struct _GtkToolButton GtkToolButton;
typedef struct _GtkToolbar GtkToolbar;
typedef struct _GtkMenuBar GtkMenuBar;
typedef struct _GtkMenu GtkMenu;
typedef struct _GtkMenuItem GtkMenuItem;
typedef struct _GtkCheckMenuItem GtkCheckMenuItem;
typedef struct _GtkImage GtkImage;
typedef struct _GtkAdjustment GtkAdjustment;
typedef struct _GtkBuilder GtkBuilder;
typedef struct _GdkPixbuf GdkPixbuf;
typedef struct _GdkScreen GdkScreen;
typedef struct _GdkEventButton GdkEventButton;
typedef struct _GdkEventKey GdkEventKey;
typedef struct _GdkEvent GdkEvent;
typedef struct _GdkDrawable GdkDrawable;
typedef struct _GdkGC GdkGC;
typedef struct _GdkPixmap GdkPixmap;
typedef struct _GdkColor GdkColor;
typedef struct _GdkRectangle GdkRectangle;
typedef struct _GdkWindow GdkWindow;
typedef struct _GdkCursor GdkCursor;
typedef struct _GdkDevice GdkDevice;
typedef struct _GdkDisplay GdkDisplay;
typedef struct _GtkCssProvider GtkCssProvider;
typedef int gboolean;
typedef int gint;
typedef unsigned int guint;
typedef long glong;
typedef unsigned long gulong;
typedef double gdouble;
typedef char gchar;
typedef unsigned char guchar;
typedef void* gpointer;
typedef const void* gconstpointer;
typedef unsigned long gsize;
typedef long gssize;
typedef unsigned int guint32;
typedef unsigned short guint16;
typedef unsigned char guint8;
typedef int gint32;
typedef short gint16;
typedef signed char gint8;
typedef long gint64;
typedef unsigned long guint64;
typedef int GCallback;
typedef void* GObject;
// GLib resource-bundle types (used by glib-compile-resources output
// in resources.c). ``GResource`` is the opaque handle returned by
// g_resource_load() / g_static_resource_get_resource(), and
// ``GStaticResource`` is the static initializer struct used by
// ``static GStaticResource static_resource = { data, size, NULL,
// NULL, NULL };``. Without these two typedefs, pycparser aborts on
// ``GResource *resources_get_resource(void);`` and on the static
// initializer (real opgui-0.12.4 resources.c case).
typedef struct _GResource GResource;
typedef struct _GStaticResource GStaticResource;
struct _GStaticResource {
    const guint8 *data;
    gsize data_size;
    GStaticResource *next;
    void *result;
    void *priv;
};
#define G_CALLBACK(f) ((GCallback)(f))
#define G_OBJECT(o) ((GObject)(o))
#define GTK_WINDOW(w) ((GtkWindow*)(w))
#define GTK_BUTTON(b) ((GtkButton*)(b))
#define GTK_LABEL(l) ((GtkLabel*)(l))
#define GTK_TOGGLE_BUTTON(b) ((GtkToggleButton*)(b))
#define GTK_STATUSBAR(s) ((GtkStatusbar*)(s))
#define GTK_TEXT_VIEW(t) ((GtkTextView*)(t))
#define GTK_TEXT_BUFFER(b) ((GtkTextBuffer*)(b))
#define GTK_TREE_VIEW(t) ((GtkTreeView*)(t))
#define GTK_SPIN_BUTTON(s) ((GtkSpinButton*)(s))
#define GTK_COMBO_BOX(c) ((GtkComboBox*)(c))
#define GTK_NOTEBOOK(n) ((GtkNotebook*)(n))
#define GTK_ENTRY(e) ((GtkEntry*)(e))
#define GTK_DIALOG(d) ((GtkDialog*)(d))
#define GTK_FILE_CHOOSER(w) ((GtkFileChooser*)(w))
#define GTK_WIDGET(w) ((GtkWidget*)(w))
#define GTK_BIN(b) ((GtkBin*)(b))
#define GTK_CONTAINER(c) ((GtkContainer*)(c))
/* GTK response codes (enum values) */
#define GTK_RESPONSE_NONE 0
#define GTK_RESPONSE_REJECT -1
#define GTK_RESPONSE_ACCEPT -2
#define GTK_RESPONSE_DELETE_EVENT -3
#define GTK_RESPONSE_OK -5
#define GTK_RESPONSE_CANCEL -6
#define GTK_RESPONSE_CLOSE -7
#define GTK_RESPONSE_YES -8
#define GTK_RESPONSE_NO -9
#define GTK_RESPONSE_APPLY -10
#define GTK_RESPONSE_HELP -11
/* GTK file chooser action enum */
#define GTK_FILE_CHOOSER_ACTION_OPEN 0
#define GTK_FILE_CHOOSER_ACTION_SAVE 1
#define GTK_FILE_CHOOSER_ACTION_SELECT_FOLDER 2
#define GTK_FILE_CHOOSER_ACTION_CREATE_FOLDER 3
#define GTK_TOOL_BUTTON(t) ((GtkToolButton*)(t))
#define GTK_TOOLBAR(t) ((GtkToolbar*)(t))
#define GTK_TREE_SELECTION(s) ((GtkTreeSelection*)(s))
extern GtkWidget* gtk_button_new(void);
extern GtkWidget* gtk_button_new_with_label(const gchar*);
extern void gtk_button_set_label(GtkButton*, const gchar*);
extern const gchar* gtk_button_get_label(GtkButton*);
extern GtkWidget* gtk_label_new(const gchar*);
extern void gtk_label_set_text(GtkLabel*, const gchar*);
extern void gtk_label_set_markup(GtkLabel*, const gchar*);
extern const gchar* gtk_label_get_text(GtkLabel*);
extern GtkWidget* gtk_entry_new(void);
extern const gchar* gtk_entry_get_text(GtkEntry*);
extern void gtk_entry_set_text(GtkEntry*, const gchar*);
extern void gtk_entry_set_max_length(GtkEntry*, gint);
extern void gtk_widget_show(GtkWidget*);
extern void gtk_widget_show_all(GtkWidget*);
extern void gtk_widget_hide(GtkWidget*);
extern void gtk_widget_set_sensitive(GtkWidget*, gboolean);
extern void gtk_widget_set_visible(GtkWidget*, gboolean);
extern void gtk_widget_destroy(GtkWidget*);
extern void gtk_widget_grab_focus(GtkWidget*);
extern void gtk_widget_queue_draw(GtkWidget*);
extern void gtk_widget_set_tooltip_text(GtkWidget*, const gchar*);
extern void gtk_main(void);
extern void gtk_main_quit(void);
extern int gtk_events_pending(void);
extern int gtk_main_iteration(void);
extern int gtk_main_iteration_do(int);
extern void gtk_init(int*, char***);
extern void gtk_statusbar_push(GtkStatusbar*, guint, const gchar*);
extern void gtk_statusbar_pop(GtkStatusbar*, guint);
extern void gtk_statusbar_remove_all(GtkStatusbar*, guint, guint);
extern gdouble gtk_spin_button_get_value(GtkSpinButton*);
extern int gtk_spin_button_get_value_as_int(GtkSpinButton*);
extern void gtk_spin_button_set_value(GtkSpinButton*, gdouble);
extern void gtk_spin_button_set_range(GtkSpinButton*, gdouble, gdouble);
extern void gtk_spin_button_set_increments(GtkSpinButton*, gdouble, gdouble);
extern gboolean gtk_toggle_button_get_active(GtkToggleButton*);
extern void gtk_toggle_button_set_active(GtkToggleButton*, gboolean);
extern gboolean gtk_toggle_tool_button_get_active(GtkToggleToolButton*);
extern void gtk_toggle_tool_button_set_active(GtkToggleToolButton*, gboolean);
extern gint gtk_combo_box_get_active(GtkComboBox*);
extern void gtk_combo_box_set_active(GtkComboBox*, gint);
extern gchar* gtk_combo_box_text_get_active_text(GtkComboBoxText*);
extern void gtk_combo_box_text_append(GtkComboBoxText*, const gchar*, const gchar*);
extern void gtk_combo_box_text_remove(GtkComboBoxText*, gint);
extern void gtk_combo_box_text_remove_all(GtkComboBoxText*);
extern GtkTextBuffer* gtk_text_view_get_buffer(GtkTextView*);
extern void gtk_text_buffer_set_text(GtkTextBuffer*, const gchar*, gint);
extern gchar* gtk_text_buffer_get_text(GtkTextBuffer*, GtkTextIter*, GtkTextIter*, gboolean);
extern void gtk_text_buffer_get_start_iter(GtkTextBuffer*, GtkTextIter*);
extern void gtk_text_buffer_get_end_iter(GtkTextBuffer*, GtkTextIter*);
extern void gtk_text_buffer_insert(GtkTextBuffer*, GtkTextIter*, const gchar*, gint);
extern void gtk_text_buffer_delete(GtkTextBuffer*, GtkTextIter*, GtkTextIter*);
extern gint gtk_text_buffer_get_line_count(GtkTextBuffer*);
extern void gtk_text_view_scroll_to_iter(GtkTextView*, GtkTextIter*, gdouble, gboolean, gdouble, gdouble);
extern GtkBuilder* gtk_builder_new(void);
extern GtkBuilder* gtk_builder_new_from_file(const gchar*);
extern GtkBuilder* gtk_builder_new_from_resource(const gchar*);
extern GObject* gtk_builder_get_object(GtkBuilder*, const gchar*);
extern void gtk_builder_connect_signals(GtkBuilder*, gpointer);
extern void gtk_window_set_title(GtkWindow*, const gchar*);
extern void gtk_window_set_default_size(GtkWindow*, gint, gint);
extern void gtk_window_set_modal(GtkWindow*, gboolean);
extern void gtk_window_close(GtkWindow*);
extern void gtk_window_present(GtkWindow*);
extern void gtk_window_resize(GtkWindow*, gint, gint);
extern void g_signal_connect(gpointer, const gchar*, GCallback, gpointer);
extern void g_signal_connect_swapped(gpointer, const gchar*, GCallback, gpointer);
extern void g_signal_handlers_disconnect_by_func(gpointer, GCallback, gpointer);
extern void g_object_unref(gpointer);
extern gpointer g_object_get_data(gpointer, const gchar*);
extern void g_object_set_data(gpointer, const gchar*, gpointer);
extern guint g_timeout_add(guint, GCallback, gpointer);
extern guint g_idle_add(GCallback, gpointer);
extern GdkPixbuf* gdk_pixbuf_new_from_resource(const gchar*, gpointer);
extern GtkCssProvider* gtk_css_provider_new(void);
extern void gtk_css_provider_load_from_resource(GtkCssProvider*, const gchar*);
extern void gtk_style_context_add_provider_for_screen(GdkScreen*, gpointer, guint);
extern GdkScreen* gdk_screen_get_default(void);
extern GtkTreeSelection* gtk_tree_view_get_selection(GtkTreeView*);
extern gboolean gtk_tree_selection_get_selected(GtkTreeSelection*, GtkListStore*, GtkTreeIter*);
extern void gtk_tree_selection_select_iter(GtkTreeSelection*, GtkTreeIter*);
extern void gtk_tree_selection_unselect_all(GtkTreeSelection*);
extern void gtk_tree_selection_set_mode(GtkTreeSelection*, gint);
extern void gtk_tree_view_insert_column_with_attributes(GtkTreeView*, gint, const gchar*, gpointer, const gchar*, gint, ...);
extern gint gtk_notebook_get_current_page(GtkNotebook*);
extern void gtk_notebook_set_current_page(GtkNotebook*, gint);
extern gint gtk_notebook_append_page(GtkNotebook*, GtkWidget*, GtkWidget*);
extern void gtk_notebook_remove_page(GtkNotebook*, gint);
extern gdouble gtk_range_get_value(GtkWidget*);
extern void gtk_range_set_value(GtkWidget*, gdouble);
extern void gtk_progress_bar_set_fraction(GtkWidget*, gdouble);
extern void gtk_progress_bar_pulse(GtkWidget*);
extern void gtk_window_set_icon(GtkWindow*, GdkPixbuf*);
#endif
""",
    "glib.h": """#ifndef __GLIB_H__
#define __GLIB_H__
typedef int gboolean;
typedef int gint;
typedef unsigned int guint;
typedef long glong;
typedef unsigned long gulong;
typedef double gdouble;
typedef char gchar;
typedef unsigned char guchar;
typedef void* gpointer;
typedef const void* gconstpointer;
typedef unsigned long gsize;
typedef long gssize;
typedef unsigned int guint32;
typedef unsigned short guint16;
typedef unsigned char guint8;
typedef int gint32;
typedef short gint16;
typedef char gint8;
typedef long gint64;
typedef unsigned long guint64;
#endif
""",
    # ---- Minimal Windows / Winsock / MMSYSTEM stubs ----
    # These are needed so that platform-specific .c files wrapped in
    # ``#ifdef _WIN32`` (such as enet's win32.c) can be preprocessed
    # and AST-parsed on a Linux host. Without these stubs, pycparser
    # sees ``SOCKET``, ``WSADATA``, ``HANDLE`` etc. used as type names
    # with no typedef in scope and aborts with ``Missing type in
    # declaration``, which causes the whole .c file's AST parse to
    # fail and the converter falls back to the pattern-based
    # extractor (which then loses every local variable declaration
    # that uses a Windows-only typedef).
    "windows.h": """#ifndef _WINDOWS_H
#define _WINDOWS_H
typedef unsigned long  DWORD;
typedef unsigned short WORD;
typedef unsigned char  BYTE;
typedef int            BOOL;
typedef void *         HANDLE;
typedef HANDLE         HMODULE;
typedef HANDLE         HINSTANCE;
typedef HANDLE         HDC;
typedef HANDLE         HWND;
typedef void *         PVOID;
typedef void *         LPVOID;
typedef char *         LPSTR;
typedef const char *   LPCSTR;
// Win32 also defines PCSTR/PCWSTR as the ``const char*`` / ``const
// wchar_t*`` pointer aliases used by setupapi.h (SetupDiGetClassDevs
// takes a PCSTR), the Win32 API documentation, and many C/C++
// samples. The ``P`` prefix means ``Pointer to`` (so PCSTR is
// ``pointer to const char``), while the ``LP`` prefix means ``Long
// pointer to`` (32-bit/64-bit pointer to). On modern Win32 they are
// the same type and the SDK provides both spellings. Without these
// aliases the preprocessed output keeps the literal ``PCSTR``
// identifier, and pycparser sees it as an undefined ID instead of a
// TYPEID, so any function-pointer typedef that uses PCSTR as a
// parameter type (e.g. ``typedef HDEVINFO (*P)(const GUID*,PCSTR,
// HWND,DWORD);``) aborts with ``Invalid declaration``.
typedef const char *   PCSTR;
typedef const wchar_t *PCWSTR;
typedef wchar_t *      PWSTR;
// ``PCTSTR`` and ``LPCTSTR`` are the TCHAR-aware versions of PCSTR /
// LPCSTR (they resolve to the ANSI or UNICODE variant based on the
// UNICODE macro). For pycparser we always define them as the ANSI
// (const char*) variant since we never define UNICODE in our fake
// headers — this matches what real Win32 builds without UNICODE
// would see.
typedef const char *   PCTSTR;
typedef const char *   LPCTSTR;
// ``HDEVINFO`` is a handle to a device information set returned by
// SetupDiGetClassDevs. The fake windows.h was missing it; without
// the typedef, the typedef'd function pointer in opgui's
// FindDevice() that uses HDEVINFO as both a return type and a
// parameter type makes pycparser abort with ``Invalid
// declaration`` on the typedef line.
typedef void *         HDEVINFO;
// ``PSP_DEVICE_INTERFACE_DETAIL_DATA_A`` is the ANSI variant of
// PSP_DEVICE_INTERFACE_DETAIL_DATA (setupapi.h). It's used in the
// FindDevice function pointer typedef list.
typedef void *         PSP_DEVICE_INTERFACE_DETAIL_DATA_A;
typedef void *         PSP_DEVICE_INTERFACE_DETAIL_DATA_W;
typedef void *         PSP_DEVICE_INTERFACE_DETAIL_DATA;
// ``PDWORD`` is a pointer to DWORD (used as an out-pointer
// parameter in SetupDiGetDeviceInterfaceDetail's Required slot).
typedef unsigned long *PDWORD;
// ``PCSTR``-style aliases for the Win32 base integer types are
// sometimes referenced by setupapi prototypes; add them for
// completeness.
typedef int            HKEY;
typedef void *         PHKEY;
// ``RECT`` and ``POINT`` are commonly used Win32 structures.
struct RECT { long left; long top; long right; long bottom; };
struct POINT { long x; long y; };
typedef int            INT;
typedef unsigned int   UINT;
typedef long           LONG;
typedef unsigned long  ULONG;
typedef short          SHORT;
typedef unsigned short USHORT;
// Win32's ``WCHAR`` is a typedef FOR ``wchar_t`` (i.e.
// ``typedef wchar_t WCHAR;``). The fake ``wchar.h`` declares
// ``typedef int wchar_t;`` so pycparser sees ``wchar_t`` as an
// alias for ``int``. Keeping the SDK's order
// (``typedef wchar_t WCHAR;``) means the AST typedef chain for
// ``WCHAR`` is ``WCHAR -> wchar_t -> int``, which is what the
// downstream Pascal translator flattens to ``WCHAR = int``.
// REVERSED order (``typedef int WCHAR;`` + ``typedef WCHAR
// wchar_t;``) would also work but is the OPPOSITE of what the
// real Win32 headers do, and would produce the wrong Pascal
// output for any code that expects ``WCHAR`` and ``wchar_t`` to
// be the same type.
typedef wchar_t        WCHAR;
typedef wchar_t *      LPWSTR;
typedef const wchar_t *LPCWSTR;
typedef unsigned long  u_long;
typedef unsigned short u_short;
typedef unsigned int   u_int;
typedef unsigned char  u_char;
typedef long      LARGE_INTEGER;
typedef long      ULARGE_INTEGER;
typedef int            BOOLEAN;
typedef unsigned long  *PULONG;
typedef unsigned long  *PDWORD;
typedef unsigned short *PWORD;
typedef unsigned char  *PBYTE;
typedef long           *PLONG;
typedef short          *PSHORT;
typedef int            *PINT;
typedef unsigned int   *PUINT;
typedef struct _GUID { unsigned long Data1; unsigned short Data2; unsigned short Data3; unsigned char Data4[8]; } GUID, *LPGUID, *REFGUID;
typedef const GUID   *LPCGUID;
typedef struct _OVERLAPPED { DWORD Internal; DWORD InternalHigh; } OVERLAPPED, *LPOVERLAPPED;
typedef struct _SECURITY_ATTRIBUTES { DWORD nLength; void *lpSecurityDescriptor; BOOL bInheritHandle; } LPSECURITY_ATTRIBUTES, *PSECURITY_ATTRIBUTES;
typedef long       __int64;
typedef long            __int32;
typedef short           __int16;
typedef char            __int8;
typedef unsigned long  __uint64;
typedef unsigned long       __uint32;
typedef unsigned short     __uint16;
typedef unsigned char      __uint8;
typedef struct _HDEVINFO { DWORD unused; } HDEVINFO, *PHDEVINFO;
typedef struct _SP_DEVICE_INTERFACE_DATA { DWORD cbSize; GUID InterfaceClassGuid; DWORD Flags; ULONG Reserved; } SP_DEVICE_INTERFACE_DATA, *PSP_DEVICE_INTERFACE_DATA;
typedef struct _SP_DEVINFO_DATA { DWORD cbSize; GUID ClassGuid; DWORD DevInst; ULONG Reserved; } SP_DEVINFO_DATA, *PSP_DEVINFO_DATA;
typedef struct _SP_DEVICE_INTERFACE_DETAIL_DATA_A { DWORD cbSize; char DevicePath[1]; } SP_DEVICE_INTERFACE_DETAIL_DATA_A, *PSP_DEVICE_INTERFACE_DETAIL_DATA_A;
typedef SP_DEVICE_INTERFACE_DETAIL_DATA_A SP_DEVICE_INTERFACE_DETAIL_DATA;
typedef PSP_DEVICE_INTERFACE_DETAIL_DATA_A PSP_DEVICE_INTERFACE_DETAIL_DATA;
typedef struct _HIDD_ATTRIBUTES { ULONG Size; USHORT VendorID; USHORT ProductID; USHORT VersionNumber; } HIDD_ATTRIBUTES, *PHIDD_ATTRIBUTES;
#define WINAPI
#define STDCALL
#define CALLBACK
#define __stdcall
#define __cdecl
#define IN
#define OUT
#define OPTIONAL
#define CONST const
#define LOBYTE(w)  ((BYTE)(w))
#define HIBYTE(w)  ((BYTE)(((WORD)(w) >> 8) & 0xFF))
#define LOWORD(w)  ((WORD)(w))
#define HIWORD(w)  ((WORD)(((DWORD)(w) >> 16) & 0xFFFF))
extern BOOL QueryPerformanceCounter(LARGE_INTEGER *);
extern BOOL QueryPerformanceFrequency(LARGE_INTEGER *);
extern DWORD GetLastError(void);
extern HANDLE CreateFile(const char *, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);
extern BOOL ReadFile(HANDLE, void *, DWORD, DWORD *, LPOVERLAPPED);
extern BOOL WriteFile(HANDLE, const void *, DWORD, DWORD *, LPOVERLAPPED);
extern BOOL CloseHandle(HANDLE);
extern HANDLE CreateEvent(LPSECURITY_ATTRIBUTES, BOOL, BOOL, const char *);
extern void Sleep(DWORD);
extern HMODULE LoadLibrary(const char *);
extern void *GetProcAddress(HMODULE, const char *);
extern BOOL FreeLibrary(HMODULE);
extern void *malloc(unsigned long);
extern void free(void *);
extern int atoi(const char *);
extern void exit(int);
extern void atexit(void (*)(void));
#define GENERIC_READ    0x80000000
#define GENERIC_WRITE   0x40000000
#define TRUE            1
#define FALSE           0
#define FILE_SHARE_READ   1
#define FILE_SHARE_WRITE  2
#define OPEN_EXISTING     3
#define FILE_FLAG_OVERLAPPED  0x40000000
#define DIGCF_PRESENT         0x2
#define DIGCF_DEVICEINTERFACE 0x10
#define DIGCF_INTERFACEDEVICE 0x10
#endif
""",
    "winsock2.h": """#ifndef _WINSOCK2_H
#define _WINSOCK2_H
#include <windows.h>
typedef int SOCKET;
#define INVALID_SOCKET  (SOCKET)(~0)
#define SOCKET_ERROR    (-1)
#define SD_RECEIVE      0
#define SD_SEND         1
#define SD_BOTH         2
#define PF_INET         2
#define AF_INET         2
#define SOCK_STREAM     1
#define SOCK_DGRAM      2
#define SOL_SOCKET      0xffff
#define SO_DEBUG        0x0001
#define SO_REUSEADDR    0x0004
#define SO_KEEPALIVE    0x0008
#define SO_DONTROUTE    0x0010
#define SO_BROADCAST    0x0020
#define SO_LINGER       0x0080
#define SO_OOBINLINE    0x0100
#define SO_SNDBUF       0x1001
#define SO_RCVBUF       0x1002
#define SO_SNDTIMEO     0x1005
#define SO_RCVTIMEO     0x1006
#define SO_ERROR        0x1007
#define IPPROTO_IP      0
#define IPPROTO_TCP     6
#define IPPROTO_UDP     17
#define IP_TTL          7
#define TCP_NODELAY     1
#define MSG_OOB         0x1
#define MSG_PEEK        0x2
#define MSG_DONTROUTE   0x4
#define MSG_PARTIAL     0x8000
#define SOMAXCONN       0x7fffffff
#define INADDR_ANY      0x00000000
#define INADDR_LOOPBACK 0x7f000001
#define INADDR_BROADCAST 0xffffffff
#define INADDR_NONE     0xffffffff
#define FIONBIO         0x8004667e
struct sockaddr {
    unsigned short sa_family;
    char sa_data[14];
};
struct in_addr {
    unsigned long s_addr;
};
struct sockaddr_in {
    short sin_family;
    unsigned short sin_port;
    struct in_addr sin_addr;
    char sin_zero[8];
};
struct hostent {
    char *h_name;
    char **h_aliases;
    short h_addrtype;
    short h_length;
    char **h_addr_list;
};
struct timeval {
    long tv_sec;
    long tv_usec;
};
typedef struct { unsigned int fd_count; int fd_array[64]; } fd_set;
#define FD_ZERO(set) do { ((fd_set *)(set))->fd_count = 0; } while (0)
#define FD_SET(s, set) do {} while (0)
#define FD_CLR(s, set) do {} while (0)
#define FD_ISSET(s, set) 0
typedef struct _WSABUF { unsigned long len; char *buf; } WSABUF, *LPWSABUF;
typedef struct _WSADATA {
    WORD wVersion;
    WORD wHighVersion;
    char szDescription[257];
    char szSystemStatus[129];
    unsigned short iMaxSockets;
    unsigned short iMaxUdpDg;
    char *lpVendorInfo;
} WSADATA;
#define MAKEWORD(a, b) ((WORD)(((BYTE)(a)) | (((WORD)((BYTE)(b))) << 8)))
int    WSAStartup(WORD, WSADATA *);
int    WSACleanup(void);
int    WSAGetLastError(void);
int    ioctlsocket(SOCKET, long, unsigned long *);
int    closesocket(SOCKET);
SOCKET socket(int, int, int);
int    bind(SOCKET, const struct sockaddr *, int);
int    listen(SOCKET, int);
SOCKET accept(SOCKET, struct sockaddr *, int *);
int    connect(SOCKET, const struct sockaddr *, int);
int    getsockname(SOCKET, struct sockaddr *, int *);
int    getpeername(SOCKET, struct sockaddr *, int *);
int    setsockopt(SOCKET, int, int, const char *, int);
int    getsockopt(SOCKET, int, int, char *, int *);
int    send(SOCKET, const char *, int, int);
int    recv(SOCKET, char *, int, int);
int    sendto(SOCKET, const char *, int, int, const struct sockaddr *, int);
int    recvfrom(SOCKET, char *, int, int, struct sockaddr *, int *);
int    shutdown(SOCKET, int);
struct hostent *gethostbyname(const char *);
struct hostent *gethostbyaddr(const char *, int, int);
unsigned long inet_addr(const char *);
char *inet_ntoa(struct in_addr);
unsigned short htons(unsigned short);
unsigned short ntohs(unsigned short);
unsigned long  htonl(unsigned long);
unsigned long  ntohl(unsigned long);
int select(int, fd_set *, fd_set *, fd_set *, const struct timeval *);
int WSASendTo(SOCKET, LPWSABUF, DWORD, DWORD *, DWORD, const struct sockaddr *, int, void *, void *);
int WSARecvFrom(SOCKET, LPWSABUF, DWORD, DWORD *, DWORD *, struct sockaddr *, int *, void *, void *);
#endif
""",
    "mmsystem.h": """#ifndef _MMSYSTEM_H
#define _MMSYSTEM_H
#include <windows.h>
int timeBeginPeriod(unsigned int);
int timeEndPeriod(unsigned int);
unsigned int timeGetTime(void);
#endif
""",
}


def main(argv=None) -> int:
    """Generate fake headers. If argv is given, use argv[0] as the
    target directory; otherwise use the module-level ROOT variable."""
    global ROOT
    if argv:
        import sys
        if isinstance(argv, list):
            ROOT = argv[0] if argv else ROOT
        elif isinstance(argv, str):
            ROOT = argv
    os.makedirs(ROOT, exist_ok=True)
    for sub in ("sys", "linux", "asm", "gtk", "glib", "gio"):
        os.makedirs(os.path.join(ROOT, sub), exist_ok=True)
    for name, content in HEADERS.items():
        path = os.path.join(ROOT, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    print(f"Generated {len(HEADERS)} fake header files in {ROOT}")
    return 0


if __name__ == "__main__":
    import sys
    # Allow: python gen_fake_includes.py [target_dir]
    if len(sys.argv) > 1:
        ROOT = sys.argv[1]
    raise SystemExit(main())


def generate(target_dir: str = None) -> int:
    """Generate fake includes in target_dir. Returns 0 on success.

    If target_dir is None, uses a platform-appropriate default:
      Linux/macOS: /tmp/fake_libc_include
      Windows: %TEMP%\\fake_libc_include
    """
    global ROOT
    import os
    import sys
    if target_dir is None:
        if sys.platform == "win32":
            target_dir = os.path.join(os.environ.get("TEMP", "C:\\Temp"),
                                       "fake_libc_include")
        else:
            target_dir = "/tmp/fake_libc_include"
    ROOT = target_dir
    return main()
