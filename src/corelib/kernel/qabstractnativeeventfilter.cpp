/****************************************************************************
**
** Copyright (C) 2012 Nokia Corporation and/or its subsidiary(-ies).
** Contact: http://www.qt-project.org/
**
** This file is part of the QtCore module of the Qt Toolkit.
**
** $QT_BEGIN_LICENSE:LGPL$
** GNU Lesser General Public License Usage
** This file may be used under the terms of the GNU Lesser General Public
** License version 2.1 as published by the Free Software Foundation and
** appearing in the file LICENSE.LGPL included in the packaging of this
** file. Please review the following information to ensure the GNU Lesser
** General Public License version 2.1 requirements will be met:
** http://www.gnu.org/licenses/old-licenses/lgpl-2.1.html.
**
** In addition, as a special exception, Nokia gives you certain additional
** rights. These rights are described in the Nokia Qt LGPL Exception
** version 1.1, included in the file LGPL_EXCEPTION.txt in this package.
**
** GNU General Public License Usage
** Alternatively, this file may be used under the terms of the GNU General
** Public License version 3.0 as published by the Free Software Foundation
** and appearing in the file LICENSE.GPL included in the packaging of this
** file. Please review the following information to ensure the GNU General
** Public License version 3.0 requirements will be met:
** http://www.gnu.org/copyleft/gpl.html.
**
** Other Usage
** Alternatively, this file may be used in accordance with the terms and
** conditions contained in a signed written agreement between you and Nokia.
**
**
**
**
**
**
** $QT_END_LICENSE$
**
****************************************************************************/

#include "qabstractnativeeventfilter.h"
#include "qabstracteventdispatcher.h"

QT_BEGIN_NAMESPACE

/*!
    \class QAbstractNativeEventFilter
    \since 5.0

    \brief The QAbstractNativeEventFilter class provides an interface for receiving native
    events, such as MSG or XCB event structs.
*/

/*!
    Creates a native event filter.

    By default this doesn't do anything. Remember to install it on the application
    object.
*/
QAbstractNativeEventFilter::QAbstractNativeEventFilter()
{
}

/*!
    Destroys the native event filter.

    This automatically removes it from the application.
*/
QAbstractNativeEventFilter::~QAbstractNativeEventFilter()
{
    QAbstractEventDispatcher *eventDispatcher = QAbstractEventDispatcher::instance();
    if (eventDispatcher)
        eventDispatcher->removeNativeEventFilter(this);
}

/*!
    \fn bool QAbstractNativeEventFilter::nativeEventFilter(const QByteArray &eventType, void *message, long *result)

    This method is called for every native event.

    \note The filter function here receives native messages,
    for example, MSG or XCB event structs.

    It is called by the QPA platform plugin. On Windows, it is called by
    the event dispatcher.

    The type of event \a eventType is specific to the platform plugin chosen at run-time,
    and can be used to cast \a message to the right type.

    On X11, \a eventType is set to "xcb_generic_event_t", and the \a message can be casted
    to a xcb_generic_event_t pointer.

    On Windows, \a eventType is set to "windows_generic_MSG" for messages sent to toplevel windows,
    and "windows_dispatcher_MSG" for system-wide messages such as messages from a registered hot key.
    In both cases, the \a message can be casted to a MSG pointer.
    The \a result pointer is only used on Windows, and corresponds to the LRESULT pointer.

    On Mac, \a eventType is set to "mac_generic_NSEvent", and the \a message can be casted to an EventRef.

    Example:
    \snippet code/src_corelib_kernel_qabstractnativeeventfilter.cpp 0
*/

QT_END_NAMESPACE
