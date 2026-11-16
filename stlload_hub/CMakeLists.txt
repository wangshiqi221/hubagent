cmake_minimum_required(VERSION 3.21)

project(stlload VERSION 0.1 LANGUAGES CXX)

set(CMAKE_AUTOUIC ON)
set(CMAKE_AUTOMOC ON)
set(CMAKE_AUTORCC ON)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

option(STLLOAD_BUILD_GUI "Build the existing Qt Widgets application" ON)
find_package(QT NAMES Qt6 Qt5 REQUIRED COMPONENTS Core)
find_package(Qt${QT_VERSION_MAJOR} REQUIRED COMPONENTS Core)
if(STLLOAD_BUILD_GUI)
    find_package(Qt${QT_VERSION_MAJOR} REQUIRED COMPONENTS Widgets LinguistTools)
endif()
set(OpenCASCADE_DIR
    "E:/opencascade/install/cmake"
    CACHE PATH "OpenCascade config path"
)

find_package(OpenCASCADE REQUIRED)

add_library(stl_geometry STATIC
    Stlsection.cpp Stlsection.h SectionTypes.h MicroBumpCleaning.h
    SmoothSplineFit.cpp SmoothSplineFit.h SectionWorkflow.cpp SectionWorkflow.h
    SurfaceFit.cpp SurfaceFit.h SurfaceJoin.cpp SurfaceJoin.h)
target_include_directories(stl_geometry PUBLIC ${CMAKE_CURRENT_SOURCE_DIR} ${OpenCASCADE_INCLUDE_DIR})
target_link_libraries(stl_geometry PUBLIC Qt${QT_VERSION_MAJOR}::Core ${OpenCASCADE_LIBRARIES})
if(MSVC)
    target_compile_options(stl_geometry PRIVATE /utf-8)
endif()

if(STLLOAD_BUILD_GUI)
set(TS_FILES stlload_zh_CN.ts)

set(PROJECT_SOURCES
        main.cpp
        mainwindow.cpp
        mainwindow.h
        mainwindow.ui
        SectionPlane.h
        SectionPlane.cpp
        curvecomparisonwindow.h
        curvecomparisonwindow.cpp
        ${TS_FILES}
)

if(${QT_VERSION_MAJOR} GREATER_EQUAL 6)
    qt_add_executable(stlload
        MANUAL_FINALIZATION
        ${PROJECT_SOURCES}
        occview.h occview.cpp
    )
# 使用 Qt 6 时，可按以下方式设置 Android 目标属性：
#    set_property(TARGET stlload APPEND PROPERTY QT_ANDROID_PACKAGE_SOURCE_DIR
#                 ${CMAKE_CURRENT_SOURCE_DIR}/android)
# 更多信息参见 https://doc.qt.io/qt-6/qt-add-executable.html#target-creation

    qt_create_translation(QM_FILES ${CMAKE_SOURCE_DIR} ${TS_FILES})
else()
    list(APPEND PROJECT_SOURCES occview.h occview.cpp)
    if(ANDROID)
        add_library(stlload SHARED
            ${PROJECT_SOURCES}
        )
# 使用 Qt 5 时，可在调用 find_package() 后按以下方式设置 Android 属性：
#    set(ANDROID_PACKAGE_SOURCE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/android")
    else()
        add_executable(stlload
            ${PROJECT_SOURCES}
        )
    endif()

    qt5_create_translation(QM_FILES ${CMAKE_SOURCE_DIR} ${TS_FILES})
endif()

target_link_libraries(stlload PRIVATE Qt${QT_VERSION_MAJOR}::Widgets stl_geometry)
if(MSVC)
    target_compile_options(stlload PRIVATE /utf-8)
endif()
target_link_libraries(stlload PRIVATE
    ${OpenCASCADE_LIBRARIES}
)

endif()

# OCCT 调试版和发布版 DLL 名称相同，但 STL 数据布局不同。
# 将导入目标选定的 DLL 放在各可执行文件旁，
# 避免 Windows 从 PATH 意外加载其他安装版本。
function(deploy_matching_runtime target)
    if(WIN32)
        add_custom_command(TARGET ${target} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                $<TARGET_RUNTIME_DLLS:${target}> $<TARGET_FILE_DIR:${target}>
            COMMAND_EXPAND_LISTS VERBATIM)
        if(TARGET Qt${QT_VERSION_MAJOR}::QWindowsIntegrationPlugin)
            add_custom_command(TARGET ${target} POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E make_directory
                    $<TARGET_FILE_DIR:${target}>/platforms
                COMMAND ${CMAKE_COMMAND} -E copy_if_different
                    $<TARGET_FILE:Qt${QT_VERSION_MAJOR}::QWindowsIntegrationPlugin>
                    $<TARGET_FILE_DIR:${target}>/platforms
                VERBATIM)
        endif()
        if(TARGET Qt${QT_VERSION_MAJOR}::QModernWindowsStylePlugin)
            add_custom_command(TARGET ${target} POST_BUILD
                COMMAND ${CMAKE_COMMAND} -E make_directory
                    $<TARGET_FILE_DIR:${target}>/styles
                COMMAND ${CMAKE_COMMAND} -E copy_if_different
                    $<TARGET_FILE:Qt${QT_VERSION_MAJOR}::QModernWindowsStylePlugin>
                    $<TARGET_FILE_DIR:${target}>/styles
                VERBATIM)
        endif()
    endif()
endfunction()
add_executable(geometry_worker geometry_worker.cpp)
target_link_libraries(geometry_worker PRIVATE stl_geometry)
if(MSVC)
    target_compile_options(geometry_worker PRIVATE /utf-8)
endif()
deploy_matching_runtime(geometry_worker)

if(STLLOAD_BUILD_GUI)
    deploy_matching_runtime(stlload)
endif()

if(STLLOAD_BUILD_GUI)
# 从 Qt 6.1 起，iOS 版 Qt 会自动设置 MACOSX_BUNDLE_GUI_IDENTIFIER。
# 开发 iOS 或 macOS 应用时，建议手动设置
# 明确且固定的应用包标识符。
if(${QT_VERSION} VERSION_LESS 6.1.0)
  set(BUNDLE_ID_OPTION MACOSX_BUNDLE_GUI_IDENTIFIER com.example.stlload)
endif()
set_target_properties(stlload PROPERTIES
    ${BUNDLE_ID_OPTION}
    MACOSX_BUNDLE_BUNDLE_VERSION ${PROJECT_VERSION}
    MACOSX_BUNDLE_SHORT_VERSION_STRING ${PROJECT_VERSION_MAJOR}.${PROJECT_VERSION_MINOR}
    MACOSX_BUNDLE TRUE
    WIN32_EXECUTABLE TRUE
)

include(GNUInstallDirs)
install(TARGETS stlload
    BUNDLE DESTINATION .
    LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
)
if(WIN32)
    install(FILES $<TARGET_RUNTIME_DLLS:stlload>
        DESTINATION ${CMAKE_INSTALL_BINDIR})
    if(TARGET Qt${QT_VERSION_MAJOR}::QWindowsIntegrationPlugin)
        install(FILES $<TARGET_FILE:Qt${QT_VERSION_MAJOR}::QWindowsIntegrationPlugin>
            DESTINATION ${CMAKE_INSTALL_BINDIR}/platforms)
    endif()
    if(TARGET Qt${QT_VERSION_MAJOR}::QModernWindowsStylePlugin)
        install(FILES $<TARGET_FILE:Qt${QT_VERSION_MAJOR}::QModernWindowsStylePlugin>
            DESTINATION ${CMAKE_INSTALL_BINDIR}/styles)
    endif()
endif()

if(QT_VERSION_MAJOR EQUAL 6)
    qt_finalize_executable(stlload)
endif()

endif() # 结束 GUI 构建分支

include(CTest)
if(BUILD_TESTING)
    add_executable(test_contour_repair test_contour_repair.cpp)
    target_link_libraries(test_contour_repair PRIVATE stl_geometry)
    if(MSVC)
        target_compile_options(test_contour_repair PRIVATE /utf-8)
    endif()
    deploy_matching_runtime(test_contour_repair)
    add_test(NAME contour_repair COMMAND test_contour_repair)
    add_executable(test_surface_fit test_surface_fit.cpp)
    target_link_libraries(test_surface_fit PRIVATE stl_geometry)
    if(MSVC)
        target_compile_options(test_surface_fit PRIVATE /utf-8)
    endif()
    deploy_matching_runtime(test_surface_fit)
    add_test(NAME surface_fit COMMAND test_surface_fit)
    add_executable(test_surface_join test_surface_join.cpp)
    target_link_libraries(test_surface_join PRIVATE stl_geometry)
    if(MSVC)
        target_compile_options(test_surface_join PRIVATE /utf-8)
    endif()
    deploy_matching_runtime(test_surface_join)
    add_test(NAME surface_join COMMAND test_surface_join)
    add_executable(verify_window_solid verify_window_solid.cpp)
    target_link_libraries(verify_window_solid PRIVATE stl_geometry)
    if(MSVC)
        target_compile_options(verify_window_solid PRIVATE /utf-8)
    endif()
    deploy_matching_runtime(verify_window_solid)
endif()
