/****************************************************************************
**
** Copyright (C) 2012 Research In Motion
** Contact: http://www.qt-project.org/
**
** This file is part of the plugins of the Qt Toolkit.
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

#include "qbbengine.h"
#include "../qnetworksession_impl.h"

#include <QDebug>
#include <QThreadStorage>
#include <QStringList>

#include <bps/netstatus.h>

#ifndef QT_NO_BEARERMANAGEMENT

#ifdef QBBENGINE_DEBUG
#define qBearerDebug qDebug
#else
#define qBearerDebug QT_NO_QDEBUG_MACRO
#endif

struct NetstatusInterfaceListCleanupHelper
{
    static inline void cleanup(netstatus_interface_list_t *list)
    {
        netstatus_free_interfaces(list);
    }
};

struct NetstatusInterfaceCleanupHelper
{
    static inline void cleanup(char *interface)
    {
        bps_free(interface);
    }
};

struct EngineInstanceHolder
{
    EngineInstanceHolder(QBBEngine *engine) :
        instance(engine) {}

    QBBEngine *instance;
};

Q_GLOBAL_STATIC(QThreadStorage<EngineInstanceHolder *>, instanceStorage);

static QNetworkConfiguration::BearerType
interfaceType(netstatus_interface_type_t type)
{
    switch (type) {
    case NETSTATUS_INTERFACE_TYPE_USB:
    case NETSTATUS_INTERFACE_TYPE_WIRED:
        return QNetworkConfiguration::BearerEthernet;

    case NETSTATUS_INTERFACE_TYPE_WIFI:
        return QNetworkConfiguration::BearerWLAN;

    case NETSTATUS_INTERFACE_TYPE_BLUETOOTH_DUN:
        return QNetworkConfiguration::BearerBluetooth;

    case NETSTATUS_INTERFACE_TYPE_CELLULAR:
        //### TODO  not sure which BearerType would be the best
        //to return here. We need to be able to get more
        //information on the bearer type in order to return
        //the exact match.
        return QNetworkConfiguration::Bearer2G;

    case NETSTATUS_INTERFACE_TYPE_VPN:
    case NETSTATUS_INTERFACE_TYPE_BB:
    case NETSTATUS_INTERFACE_TYPE_UNKNOWN:
        break;
    }

    return QNetworkConfiguration::BearerUnknown;
}

static QString idForName(const QString &name)
{
    return QStringLiteral("bps:") + name;
}

QT_BEGIN_NAMESPACE


QBBEngine::QBBEngine(QObject *parent) :
    QBearerEngineImpl(parent),
    pollingRequired(false),
    initialized(false)
{
}

QBBEngine::~QBBEngine()
{
}


QString QBBEngine::getInterfaceFromId(const QString &id)
{
    const QMutexLocker locker(&mutex);

    return configurationInterface.value(id);
}

bool QBBEngine::hasIdentifier(const QString &id)
{
    const QMutexLocker locker(&mutex);

    return configurationInterface.contains(id);
}

void QBBEngine::connectToId(const QString &id)
{
    Q_EMIT connectionError(id, OperationNotSupported);
}

void QBBEngine::disconnectFromId(const QString &id)
{
    Q_EMIT connectionError(id, OperationNotSupported);
}

void QBBEngine::initialize()
{
    if (initialized) {
        qWarning() << Q_FUNC_INFO << "called, but instance already initialized.";
        return;
    }

    instanceStorage()->setLocalData(new EngineInstanceHolder(this));

    if (netstatus_request_events(0) != BPS_SUCCESS) {
        qWarning() << Q_FUNC_INFO << "cannot register for network events. Polling enabled.";

        const QMutexLocker locker(&pollingMutex);
        pollingRequired = true;
    } else {
        QAbstractEventDispatcher::instance()->installEventFilter(this);
    }

    doRequestUpdate();
}

void QBBEngine::requestUpdate()
{
    doRequestUpdate();
}

void QBBEngine::doRequestUpdate()
{
    qBearerDebug() << Q_FUNC_INFO << "entered method";

    netstatus_interface_list_t interfaceList;

    if ((netstatus_get_interfaces(&interfaceList)) != BPS_SUCCESS) {
        qBearerDebug() << Q_FUNC_INFO << "cannot retrieve interface list";
        return;
    }

    const QScopedPointer<netstatus_interface_list_t,
          NetstatusInterfaceListCleanupHelper> holder(&interfaceList);

    QSet<QString> currentConfigurations;

    for (int i = 0; i < interfaceList.num_interfaces; i++) {
        const char *interface = interfaceList.interfaces[i];

        qBearerDebug() << Q_FUNC_INFO << "discovered interface" << interface;

        updateConfiguration(interface);

        currentConfigurations << idForName(QString::fromLatin1(interface));
    }

    QMutexLocker locker(&mutex);

    const QStringList keys = accessPointConfigurations.uniqueKeys();

    locker.unlock();

    Q_FOREACH (const QString &id, keys) {
        if (!currentConfigurations.contains(id))
            removeConfiguration(id);
    }

    Q_EMIT updateCompleted();
}

QNetworkSession::State QBBEngine::sessionStateForId(const QString &id)
{
    const QMutexLocker locker(&mutex);

    QNetworkConfigurationPrivatePointer ptr = accessPointConfigurations.value(id);

    if (!ptr || !ptr->isValid)
        return QNetworkSession::Invalid;

    if ((ptr->state & QNetworkConfiguration::Active) == QNetworkConfiguration::Active)
        return QNetworkSession::Connected;
    else if ((ptr->state & QNetworkConfiguration::Discovered) == QNetworkConfiguration::Discovered)
        return QNetworkSession::Disconnected;
    else if ((ptr->state & QNetworkConfiguration::Defined) == QNetworkConfiguration::Defined)
        return QNetworkSession::NotAvailable;
    else if ((ptr->state & QNetworkConfiguration::Undefined) == QNetworkConfiguration::Undefined)
        return QNetworkSession::NotAvailable;

    return QNetworkSession::Invalid;
}

QNetworkConfigurationManager::Capabilities QBBEngine::capabilities() const
{
    return QNetworkConfigurationManager::ForcedRoaming;
}

QNetworkSessionPrivate *QBBEngine::createSessionBackend()
{
    return new QNetworkSessionPrivateImpl;
}

QNetworkConfigurationPrivatePointer QBBEngine::defaultConfiguration()
{
    char *interface = 0;

    if (netstatus_get_default_interface(&interface) != BPS_SUCCESS)
        return QNetworkConfigurationPrivatePointer();

    if (!interface)
        return QNetworkConfigurationPrivatePointer();

    const QScopedPointer<char, NetstatusInterfaceCleanupHelper> holder(interface);

    const QString id = idForName(QString::fromLatin1(interface));

    const QMutexLocker locker(&mutex);

    if (accessPointConfigurations.contains(id)) {
        qBearerDebug() << Q_FUNC_INFO << "found default interface:" << id;

        return accessPointConfigurations.value(id);
    }

    return QNetworkConfigurationPrivatePointer();
}

bool QBBEngine::requiresPolling() const
{
    const QMutexLocker locker(&pollingMutex);

    return pollingRequired;
}

bool QBBEngine::nativeEventFilter(const QByteArray &eventType, void *message, long *result)
{
    Q_UNUSED(eventType);
    Q_UNUSED(result);

    bps_event_t * const event = static_cast<bps_event_t *>(message);

    Q_ASSERT(event);

    if (bps_event_get_domain(event) == netstatus_get_domain()) {
        qBearerDebug() << Q_FUNC_INFO << "got update request.";
        doRequestUpdate();
    }

    return false;
}

void QBBEngine::updateConfiguration(const char *interface)
{
    netstatus_interface_details_t *details = 0;

    if (netstatus_get_interface_details(interface, &details) != BPS_SUCCESS) {
        qBearerDebug() << Q_FUNC_INFO << "cannot retrieve details for interface" << interface;

        return;
    }

    const QString name = QString::fromLatin1(netstatus_interface_get_name(details));
    const QString id = idForName(name);


    const int numberOfIpAddresses = netstatus_interface_get_num_ip_addresses(details);
    const bool isConnected = netstatus_interface_is_connected(details);
    const netstatus_interface_type_t type = netstatus_interface_get_type(details);

    netstatus_free_interface_details(&details);

    QNetworkConfiguration::StateFlags state = QNetworkConfiguration::Defined;

    if (isConnected && (numberOfIpAddresses > 0))
        state |= QNetworkConfiguration::Active;

    QMutexLocker locker(&mutex);

    if (accessPointConfigurations.contains(id)) {
        QNetworkConfigurationPrivatePointer ptr = accessPointConfigurations.value(id);

        bool changed = false;

        QMutexLocker ptrLocker(&ptr->mutex);

        if (!ptr->isValid) {
            ptr->isValid = true;
            changed = true;
        }

        if (ptr->name != name) {
            ptr->name = name;
            changed = true;
        }

        if (ptr->id != id) {
            ptr->id = id;
            changed = true;
        }

        if (ptr->state != state) {
            ptr->state = state;
            changed = true;
        }

        ptrLocker.unlock();

        locker.unlock();

        if (changed) {
            qBearerDebug() << Q_FUNC_INFO << "configuration changed:" << interface;

            Q_EMIT configurationChanged(ptr);
        } else {
            qBearerDebug() << Q_FUNC_INFO << "configuration has not changed.";
        }

        return;
    }

    QNetworkConfigurationPrivatePointer ptr(new QNetworkConfigurationPrivate);

    ptr->name = name;
    ptr->isValid = true;
    ptr->id = id;
    ptr->state = state;
    ptr->type = QNetworkConfiguration::InternetAccessPoint;
    ptr->bearerType = interfaceType(type);

    accessPointConfigurations.insert(id, ptr);
    configurationInterface.insert(id, name);

    locker.unlock();

    qBearerDebug() << Q_FUNC_INFO << "configuration added:" << interface;

    Q_EMIT configurationAdded(ptr);
}

void QBBEngine::removeConfiguration(const QString &id)
{
    QMutexLocker locker(&mutex);

    QNetworkConfigurationPrivatePointer ptr =
        accessPointConfigurations.take(id);

    configurationInterface.remove(ptr->id);

    locker.unlock();

    Q_EMIT configurationRemoved(ptr);
}

QT_END_NAMESPACE

#endif // QT_NO_BEARERMANAGEMENT
